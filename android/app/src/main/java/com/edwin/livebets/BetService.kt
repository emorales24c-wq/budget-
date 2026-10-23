package com.edwin.livebets

import android.app.AlarmManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.content.res.ColorStateList
import android.graphics.Color
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.PowerManager
import android.widget.RemoteViews
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
        private const val PREGAME_MAX_MS = 30 * 60_000L   // check at least this often before tip-off
        private const val TIPOFF_EARLY_MS = 2 * 60_000L   // wake up this long before tip-off

        /** The running service, so a cleared notification can ask it to post again. */
        @Volatile var running: BetService? = null

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
    private val finalIds = mutableSetOf<Int>()   // ids showing a won/lost result

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
        running = this
    }

    /** A bet notification was swiped away while the bet is still open: put it back. */
    fun repostSoon() {
        handler.removeCallbacks(tick)
        handler.postDelayed(tick, 1000)
    }

    /** The pre-game alarm fired: start polling now. */
    fun wakeAndPoll() {
        stayAwake()
        handler.removeCallbacks(tick)
        handler.post(tick)
    }

    // Short, renewed wake lock: held only while games are live, never left dangling
    private fun stayAwake() {
        val wl = wakeLock ?: getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "livebets:poll")
            .apply { setReferenceCounted(false) }
            .also { wakeLock = it }
        wl.acquire(2 * 60_000L)
    }

    private fun sleep() {
        wakeLock?.let { if (it.isHeld) it.release() }
    }

    // Exact alarms still fire in Doze, unlike Handler delays
    private fun scheduleWake(atMs: Long) {
        val am = getSystemService(AlarmManager::class.java)
        val pi = PendingIntent.getBroadcast(
            this, 0, Intent(this, WakeReceiver::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        if (Build.VERSION.SDK_INT < 31 || am.canScheduleExactAlarms()) {
            am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, atMs, pi)
        } else {
            am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, atMs, pi)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val n = NotificationCompat.Builder(this, CH_SERVICE)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("Live Bets")
            .setContentText("Siguiendo tus apuestas")
            .setContentIntent(openApp())
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .build()
        // dataSync services are cut off after 6 h a day on Android 15; specialUse has no limit
        val type = when {
            Build.VERSION.SDK_INT >= 34 -> ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
            Build.VERSION.SDK_INT >= 29 -> ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            else -> 0
        }
        ServiceCompat.startForeground(this, SERVICE_ID, n, type)
        // Bet list changed (or alarm fired): refresh right away
        wakeAndPoll()
        return START_STICKY
    }

    override fun onTimeout(startId: Int, fgsType: Int) {
        stopTracking()
    }

    override fun onDestroy() {
        running = null
        handler.removeCallbacks(tick)
        thread.quitSafely()
        sleep()
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
        // Bets deleted in the app (results moved to the history keep their notification)
        (posted - ids).forEach { if (it !in finalIds) nm.cancel(it); posted.remove(it) }
        settled.retainAll(bets.map { it.id }.toSet())

        val open = bets.filter { it.id !in settled }
        if (open.isEmpty()) return null

        val games = open.flatMap { b -> b.legs.map { it.game } }.distinct().associateWith { id ->
            try { Espn.summary(id) } catch (e: Exception) { null }
        }

        for (b in open) show(b, games)

        // Live: keep the CPU awake and poll every 20 s
        if (games.values.any { it?.state == "in" }) {
            stayAwake()
            return LIVE_MS
        }
        val now = System.currentTimeMillis()
        // ESPN unreachable (no signal?): retry in a minute without holding the CPU awake
        if (games.values.any { it == null }) {
            scheduleWake(now + 60_000L)
            sleep()
            return 60_000L
        }
        // Before tip-off: sleep until just before the first game starts
        val firstStart = games.values.mapNotNull { it?.start }.filter { it > 0 }.minOrNull()
        val delay = ((firstStart ?: 0L) - TIPOFF_EARLY_MS - now).coerceIn(60_000L, PREGAME_MAX_MS)
        scheduleWake(now + delay)
        sleep()
        return delay
    }

    private class LegResult(
        val leg: Leg, val game: LiveGame?, val name: String, val cur: Int, val status: String, val photoId: String,
    )

    private fun evalLeg(l: Leg, g: LiveGame?): LegResult {
        val p = g?.let { Espn.findPlayer(it, l.player) }
        val cur = p?.let { pl -> Espn.STATS[l.stat]?.sumOf { pl.stats[it] ?: 0 } } ?: 0
        val final = g?.state == "post"
        val status = when {
            final && p == null -> "void"
            l.side == "over" && cur >= target(l) -> "won"
            l.side == "under" && cur > l.line -> "lost"
            final && cur.toDouble() == l.line -> "push"
            final -> if (l.side == "over") "lost" else "won"
            g?.state == "in" -> "live"
            else -> "pending"
        }
        // "Jaren Jackson Jr." -> "Jackson"
        val words = (p?.name ?: l.player).trim().split(Regex("\\s+"))
        val name = words.filterIndexed { i, w -> i == 0 || !SUFFIX.matches(w) }.last()
        return LegResult(l, g, name, cur, status, Photos.url(l.photo, l.playerId.ifEmpty { p?.id ?: "" }))
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

    private val DONE = setOf("won", "lost", "push", "void")
    private val SUFFIX = Regex("(?i)^(jr|sr|ii|iii|iv)\\.?$")
    private val GREEN = Color.parseColor("#3ECF8E")
    private val RED = Color.parseColor("#F87171")
    private val ACCENT = Color.parseColor("#E07B54")

    // Color only ever means bet state: red is reserved for a lost bet, so team colors aren't used
    private fun legColor(r: LegResult): Int = when (r.status) {
        "won" -> GREEN
        "lost" -> RED
        "push", "void" -> Color.GRAY
        else -> ACCENT
    }

    // A whole-number over line is a sportsbook milestone: "2" means 2 or more (no push)
    private fun atLeast(l: Leg) = l.side == "over" && l.line % 1.0 == 0.0
    private fun sideTxt(l: Leg) =
        if (atLeast(l)) "${lineTxt(l)}+ ${l.stat}" else "${if (l.side == "over") "O" else "U"}${lineTxt(l)} ${l.stat}"
    // Over: what it needs to hit. Under: the count that loses it.
    private fun target(l: Leg) = if (atLeast(l)) l.line.toInt() else floor(l.line).toInt() + 1

    private fun RemoteViews.bar(id: Int, max: Int, progress: Int, color: Int) {
        setProgressBar(id, max, progress, false)
        if (Build.VERSION.SDK_INT >= 31) {
            setColorStateList(id, "setProgressTintList", ColorStateList.valueOf(color))
            setColorStateList(id, "setProgressBackgroundTintList", ColorStateList.valueOf(Color.argb(60, 128, 128, 128)))
        }
    }

    private fun RemoteViews.photo(id: Int, photoId: String) {
        val bmp = Photos.get(photoId)
        if (bmp != null) setImageViewBitmap(id, bmp) else setImageViewResource(id, R.drawable.ic_launcher)
    }

    private fun show(b: Bet, games: Map<String, LiveGame?>) {
        val rs = b.legs.map { evalLeg(it, games[it.game]) }
        val hits = rs.count { it.status == "won" }
        val status = if (!b.parlay) rs.first().status else when {
            rs.any { it.status == "lost" } -> "lost"
            rs.all { it.status in DONE } -> if (hits > 0) "won" else "push"
            rs.any { it.status == "live" || it.status == "won" } -> "live"
            else -> "pending"
        }
        val settledNow = status in DONE

        var money = ""
        if (settledNow && b.stake > 0) {
            val o = b.odds.toDoubleOrNull() ?: if (b.parlay) 0.0 else -110.0
            val win = when {
                o > 0 -> b.stake * o / 100
                o < 0 -> b.stake * 100 / -o
                else -> 0.0
            }
            money = when (status) {
                "won" -> if (win > 0) " · +$%.2f".format(win) else ""
                "lost" -> " · -$%.2f".format(b.stake)
                else -> ""
            }
        }

        val small = RemoteViews(packageName, R.layout.notif_small)
        val big: RemoteViews
        val title: String
        val text: String
        val color: Int

        if (!b.parlay) {
            val r = rs.first()
            color = legColor(r)
            title = "${icon(status)} ${r.name}  ${r.cur} / ${lineTxt(r.leg)} ${r.leg.stat}"
            text = gameTxt(r) + money
            val max = target(r.leg)

            small.photo(R.id.photo, r.photoId)
            small.setTextViewText(R.id.name, "${icon(status)} ${r.name}")
            small.setTextViewText(R.id.count, "${r.cur} / ${lineTxt(r.leg)} ${r.leg.stat}")
            small.setTextColor(R.id.count, color)
            small.bar(R.id.bar, max, min(r.cur, max), color)
            small.setTextViewText(R.id.info, text)

            big = RemoteViews(packageName, R.layout.notif_big)
            big.photo(R.id.photo, r.photoId)
            big.setTextViewText(R.id.name, "${icon(status)} ${r.leg.player}")
            big.setTextViewText(R.id.count, r.cur.toString())
            big.setTextColor(R.id.count, color)
            big.setTextViewText(R.id.line, "/ ${sideTxt(r.leg)}")
            big.bar(R.id.bar, max, min(r.cur, max), color)
            big.setTextViewText(R.id.info, text)
        } else {
            color = when (status) { "won" -> GREEN; "lost" -> RED; else -> ACCENT }
            title = "${icon(status)} Parlay  $hits / ${rs.size} piernas"
            // Collapsed view follows the first leg still in play
            val next = rs.firstOrNull { it.status !in DONE }
            text = (next?.let { "${it.name} ${it.cur}/${lineTxt(it.leg)} ${it.leg.stat} · ${gameTxt(it)}" }
                ?: rs.joinToString("  ") { "${icon(it.status)} ${it.name}" }) + money

            small.photo(R.id.photo, (next ?: rs.first()).photoId)
            small.setTextViewText(R.id.name, "${icon(status)} Parlay")
            small.setTextViewText(R.id.count, "$hits / ${rs.size}")
            small.setTextColor(R.id.count, color)
            small.bar(R.id.bar, rs.size, hits, color)
            small.setTextViewText(R.id.info, text)

            big = RemoteViews(packageName, R.layout.notif_parlay_big)
            big.setTextViewText(R.id.name, "${icon(status)} Parlay · ${rs.size} piernas$money")
            big.setTextViewText(R.id.count, "$hits / ${rs.size} piernas")
            big.setTextColor(R.id.count, color)
            big.bar(R.id.bar, rs.size, hits, color)
            big.removeAllViews(R.id.legs)
            for (r in rs.take(5)) {
                val row = RemoteViews(packageName, R.layout.notif_leg)
                val c = legColor(r)
                row.photo(R.id.photo, r.photoId)
                row.setTextViewText(R.id.name, "${if (r.status in DONE) icon(r.status) + " " else ""}${r.name} · ${sideTxt(r.leg)}")
                row.setTextViewText(R.id.count, "${r.cur} / ${lineTxt(r.leg)}")
                row.setTextColor(R.id.count, c)
                row.bar(R.id.bar, target(r.leg), min(r.cur, target(r.leg)), c)
                big.addView(R.id.legs, row)
            }
        }

        val nb = NotificationCompat.Builder(this, if (settledNow) CH_RESULT else CH_LIVE)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle(title)
            .setContentText(text)
            .setColor(color)
            .setStyle(NotificationCompat.DecoratedCustomViewStyle())
            .setCustomContentView(small)
            .setCustomBigContentView(big)
            .setContentIntent(openApp())
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setCategory(NotificationCompat.CATEGORY_PROGRESS)
            .setShowWhen(false)
            .setOnlyAlertOnce(!settledNow)
            .setOngoing(!settledNow)
            .setAutoCancel(settledNow)
            .setSortKey(b.id.toString())

        val id = notifId(b)
        if (settledNow) {
            settled.add(b.id)
            finalIds.add(id)
        } else {
            nb.setDeleteIntent(
                PendingIntent.getBroadcast(
                    this, id, Intent(this, ClearedReceiver::class.java), PendingIntent.FLAG_IMMUTABLE
                )
            )
        }

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
