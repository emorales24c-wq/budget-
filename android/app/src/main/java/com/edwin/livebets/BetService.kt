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

        val games = open.flatMap { b -> b.legs.map { it.game } }.distinct().associateWith { id ->
            try { Espn.summary(id) } catch (e: Exception) { null }
        }

        for (b in open) show(b, games)
        val anyLive = games.values.any { it?.state == "in" }
        return if (anyLive || games.values.any { it == null }) LIVE_MS else PREGAME_MS
    }

    private class LegResult(val leg: Leg, val game: LiveGame?, val name: String, val cur: Int, val status: String)

    private fun evalLeg(l: Leg, g: LiveGame?): LegResult {
        val p = g?.let { Espn.findPlayer(it, l.player) }
        val cur = p?.let { pl -> Espn.STATS[l.stat]?.sumOf { pl.stats[it] ?: 0 } } ?: 0
        val final = g?.state == "post"
        val status = when {
            final && p == null -> "void"
            l.side == "over" && cur > l.line -> "won"
            l.side == "under" && cur > l.line -> "lost"
            final && cur.toDouble() == l.line -> "push"
            final -> if (l.side == "over") "lost" else "won"
            g?.state == "in" -> "live"
            else -> "pending"
        }
        val name = (p?.name ?: l.player).trim().split(Regex("\\s+")).last()
        return LegResult(l, g, name, cur, status)
    }

    private fun icon(status: String) = when (status) {
        "won" -> "✅"; "lost" -> "❌"; "push" -> "➖"; "void" -> "⚪"; "live" -> "🔴"; else -> "⏳"
    }

    private fun lineTxt(l: Leg) = if (l.line % 1.0 == 0.0) l.line.toInt().toString() else l.line.toString()

    private fun gameTxt(r: LegResult) = when {
        r.game == null -> r.leg.gameName
        r.game.state == "pre" -> "${r.leg.gameName} · ${r.game.detail}"
        else -> "${r.game.detail} · ${r.game.score}"
    }

    private fun show(b: Bet, games: Map<String, LiveGame?>) {
        val rs = b.legs.map { evalLeg(it, games[it.game]) }
        val done = setOf("won", "lost", "push", "void")
        val status: String
        val title: String
        val text: String
        val progress: Int
        val max: Int
        var lines: List<String>? = null

        if (!b.parlay) {
            val r = rs.first()
            status = r.status
            title = "${icon(status)} ${r.name}  ${r.cur} / ${lineTxt(r.leg)} ${r.leg.stat}"
            text = gameTxt(r)
            max = floor(r.leg.line).toInt() + 1    // what the over needs to hit
            progress = min(r.cur, max)
        } else {
            val hits = rs.count { it.status == "won" }
            status = when {
                rs.any { it.status == "lost" } -> "lost"
                rs.all { it.status in done } -> if (hits > 0) "won" else "push"
                rs.any { it.status == "live" || it.status == "won" } -> "live"
                else -> "pending"
            }
            title = "${icon(status)} Parlay  $hits / ${rs.size} ✅"
            // Collapsed view: the first leg still in play
            val next = rs.firstOrNull { it.status !in done }
            text = next?.let { "${it.name} ${it.cur}/${lineTxt(it.leg)} ${it.leg.stat} · ${gameTxt(it)}" }
                ?: rs.joinToString("  ") { "${icon(it.status)} ${it.name}" }
            lines = rs.map {
                val mark = if (it.status in done) icon(it.status) else "•"
                "$mark ${it.name}  ${it.cur} / ${if (it.leg.side == "over") "O" else "U"}${lineTxt(it.leg)} ${it.leg.stat}"
            }
            max = rs.size
            progress = hits
        }

        val settledNow = status in done
        val color = when (status) {
            "won" -> Color.parseColor("#3ECF8E"); "lost" -> Color.parseColor("#F87171")
            else -> Color.parseColor("#E07B54")
        }
        val sub = if (b.parlay) "Parlay ${rs.size} piernas"
            else rs.first().leg.let { "${if (it.side == "over") "O" else "U"}${lineTxt(it)} ${it.stat}" }

        val nb = NotificationCompat.Builder(this, if (settledNow) CH_RESULT else CH_LIVE)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle(title)
            .setContentText(text)
            .setSubText(sub)
            .setColor(color)
            .setContentIntent(openApp())
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setCategory(NotificationCompat.CATEGORY_PROGRESS)
            .setShowWhen(false)
            .setOnlyAlertOnce(!settledNow)
            .setOngoing(!settledNow)
            .setAutoCancel(settledNow)
            .setSortKey(b.id.toString())

        lines?.let { ls -> nb.setStyle(NotificationCompat.InboxStyle().also { st -> ls.forEach { st.addLine(it) } }) }
        if (!settledNow) nb.setProgress(max, progress, false)
        if (settledNow) {
            if (b.stake > 0) {
                val o = b.odds.toDoubleOrNull() ?: if (b.parlay) 0.0 else -110.0
                val win = when {
                    o > 0 -> b.stake * o / 100
                    o < 0 -> b.stake * 100 / -o
                    else -> 0.0
                }
                val money = when (status) {
                    "won" -> if (win > 0) " · +$%.2f".format(win) else ""
                    "lost" -> " · -$%.2f".format(b.stake)
                    else -> ""
                }
                nb.setContentText(text + money)
            }
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
