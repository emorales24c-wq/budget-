package com.edwin.livebets

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Color
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import kotlin.math.floor
import kotlin.math.min

/**
 * Polls ESPN while there are open bets and keeps one ongoing notification per bet,
 * with a real progress bar, on the lock screen.
 */
class BetService : Service() {

    companion object {
        private const val CH_SERVICE = "service"
        private const val CH_LIVE = "live"
        private const val CH_RESULT = "result"
        private const val SERVICE_ID = 1
        private const val LIVE_MS = 20_000L
        private const val PREGAME_MS = 5 * 60_000L

        fun start(ctx: Context) {
            val i = Intent(ctx, BetService::class.java)
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i) else ctx.startService(i)
        }
    }

    private lateinit var thread: HandlerThread
    private lateinit var handler: Handler
    private lateinit var nm: NotificationManager
    private var wakeLock: PowerManager.WakeLock? = null
    private val settled = mutableSetOf<Long>()   // bets whose final notification was posted
    private val posted = mutableSetOf<Int>()     // notification ids currently shown

    private val tick = object : Runnable {
        override fun run() {
            val next = poll()
            if (next == null) stopTracking() else handler.postDelayed(this, next)
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        nm = getSystemService(NotificationManager::class.java)
        createChannels()
        thread = HandlerThread("bets").apply { start() }
        handler = Handler(thread.looper)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val n = NotificationCompat.Builder(this, CH_SERVICE)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("Live Bets")
            .setContentText("Siguiendo tus apuestas")
            .setContentIntent(openApp())
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .build()
        ServiceCompat.startForeground(
            this, SERVICE_ID, n,
            if (Build.VERSION.SDK_INT >= 29) ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC else 0
        )
        if (wakeLock == null) {
            wakeLock = getSystemService(PowerManager::class.java)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "livebets:poll")
                .apply { acquire(6 * 60 * 60_000L) }
        }
        // Bet list changed: refresh right away
        handler.removeCallbacks(tick)
        handler.post(tick)
        return START_STICKY
    }

    override fun onTimeout(startId: Int, fgsType: Int) {
        stopTracking()
    }

    override fun onDestroy() {
        handler.removeCallbacks(tick)
        thread.quitSafely()
        wakeLock?.let { if (it.isHeld) it.release() }
        super.onDestroy()
    }

    private fun stopTracking() {
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    /** Returns the delay until the next poll, or null when nothing is left to track. */
    private fun poll(): Long? {
        val bets = BetStore.load(this)
        val ids = bets.map { notifId(it) }.toSet()
        // Bets deleted in the app
        (posted - ids).forEach { nm.cancel(it); posted.remove(it) }
        settled.retainAll(bets.map { it.id }.toSet())

        val open = bets.filter { it.id !in settled }
        if (open.isEmpty()) return null

        val games = open.map { it.game }.distinct().associateWith { id ->
            try { Espn.summary(id) } catch (e: Exception) { null }
        }

        var anyLive = false
        for (b in open) {
            val g = games[b.game]
            if (g?.state == "in") anyLive = true
            show(b, g)
        }
        return if (anyLive || games.values.any { it == null }) LIVE_MS else PREGAME_MS
    }

    private fun show(b: Bet, g: LiveGame?) {
        val p = g?.let { Espn.findPlayer(it, b.player) }
        val cur = p?.let { pl -> Espn.STATS[b.stat]?.sumOf { pl.stats[it] ?: 0 } } ?: 0
        val final = g?.state == "post"
        val status = when {
            final && p == null -> "void"
            b.side == "over" && cur > b.line -> "won"
            b.side == "under" && cur > b.line -> "lost"
            final && cur.toDouble() == b.line -> "push"
            final -> if (b.side == "over") "lost" else "won"
            g?.state == "in" -> "live"
            else -> "pending"
        }

        val name = (p?.name ?: b.player).trim().split(Regex("\\s+")).last()
        val side = if (b.side == "over") "O" else "U"
        val lineTxt = if (b.line % 1.0 == 0.0) b.line.toInt().toString() else b.line.toString()
        val target = floor(b.line).toInt() + 1    // what the over needs to hit
        val done = status in setOf("won", "lost", "push", "void")

        val icon = when (status) {
            "won" -> "✅"; "lost" -> "❌"; "push" -> "➖"; "void" -> "⚪"; "live" -> "🔴"; else -> "⏳"
        }
        val title = "$icon $name  $cur / $lineTxt ${b.stat}"
        val text = when {
            g == null -> b.gameName
            g.state == "pre" -> "${b.gameName} · ${g.detail}"
            else -> "${g.detail} · ${g.score}"
        }
        val color = when (status) {
            "won" -> Color.parseColor("#3ECF8E"); "lost" -> Color.parseColor("#F87171")
            else -> Color.parseColor("#E07B54")
        }

        val nb = NotificationCompat.Builder(this, if (done) CH_RESULT else CH_LIVE)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle(title)
            .setContentText(text)
            .setSubText("$side$lineTxt ${b.stat}")
            .setColor(color)
            .setColorized(false)
            .setContentIntent(openApp())
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setCategory(NotificationCompat.CATEGORY_PROGRESS)
            .setShowWhen(false)
            .setOnlyAlertOnce(!done)
            .setOngoing(!done)
            .setAutoCancel(done)
            .setSortKey(b.id.toString())

        if (!done) nb.setProgress(target, min(cur, target), false)
        if (done) {
            val money = if (b.stake > 0) {
                val o = b.odds.toDoubleOrNull() ?: -110.0
                val win = if (o > 0) b.stake * o / 100 else b.stake * 100 / -o
                when (status) {
                    "won" -> " · +$%.2f".format(win)
                    "lost" -> " · -$%.2f".format(b.stake)
                    else -> ""
                }
            } else ""
            nb.setContentText("${g?.detail ?: ""} · ${g?.score ?: ""}$money")
            settled.add(b.id)
        }

        val id = notifId(b)
        nm.notify(id, nb.build())
        posted.add(id)
    }

    private fun notifId(b: Bet) = (b.id % Int.MAX_VALUE).toInt().let { if (it == SERVICE_ID) 2 else it }

    private fun openApp(): PendingIntent =
        PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

    private fun createChannels() {
        // Hidden channel for the mandatory foreground notification
        nm.createNotificationChannel(
            NotificationChannel(CH_SERVICE, "Servicio", NotificationManager.IMPORTANCE_MIN).apply {
                setShowBadge(false)
            }
        )
        // Live progress: shows on the lock screen but never makes noise
        nm.createNotificationChannel(
            NotificationChannel(CH_LIVE, "Apuestas en vivo", NotificationManager.IMPORTANCE_DEFAULT).apply {
                setSound(null, null)
                enableVibration(false)
                setShowBadge(false)
                lockscreenVisibility = Notification.VISIBILITY_PUBLIC
            }
        )
        // Won / lost: sound + vibration
        nm.createNotificationChannel(
            NotificationChannel(CH_RESULT, "Resultados", NotificationManager.IMPORTANCE_HIGH).apply {
                lockscreenVisibility = Notification.VISIBILITY_PUBLIC
            }
        )
    }
}
