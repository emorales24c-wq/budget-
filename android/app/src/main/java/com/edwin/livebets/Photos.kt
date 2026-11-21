package com.edwin.livebets

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.PorterDuff
import android.graphics.PorterDuffXfermode
import android.graphics.Rect
import android.graphics.RectF
import java.net.HttpURLConnection
import java.net.URL

/** ESPN player headshots, cropped to a circle and cached in memory. */
object Photos {
    private const val SIZE = 160
    private const val RETRY_MS = 5 * 60_000L
    private val cache = mutableMapOf<String, Bitmap>()
    private val failedAt = mutableMapOf<String, Long>()

    fun url(photo: String, id: String): String = when {
        photo.isNotEmpty() -> photo
        id.isNotEmpty() -> "https://a.espncdn.com/i/headshots/nba/players/full/$id.png"
        else -> ""
    }

    fun get(url: String): Bitmap? {
        if (url.isEmpty()) return null
        cache[url]?.let { return it }
        // Don't hammer a failing URL every poll, but do try again later
        failedAt[url]?.let { if (System.currentTimeMillis() - it < RETRY_MS) return null }
        return try {
            val c = URL(url).openConnection() as HttpURLConnection
            c.connectTimeout = 8000
            c.readTimeout = 8000
            c.instanceFollowRedirects = true
            c.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android) LiveBets")
            val src = try {
                if (c.responseCode != 200) throw RuntimeException("HTTP ${c.responseCode}")
                c.inputStream.use { BitmapFactory.decodeStream(it) } ?: throw RuntimeException("decode")
            } finally {
                c.disconnect()
            }
            circle(src).also { cache[url] = it; failedAt.remove(url) }
        } catch (e: Exception) {
            failedAt[url] = System.currentTimeMillis()
            null
        }
    }

    // Square crop from the top (headshots are wider than tall), then a round mask
    private fun circle(src: Bitmap): Bitmap {
        val side = minOf(src.width, src.height)
        val left = (src.width - side) / 2
        val out = Bitmap.createBitmap(SIZE, SIZE, Bitmap.Config.ARGB_8888)
        val canvas = Canvas(out)
        val paint = Paint(Paint.ANTI_ALIAS_FLAG or Paint.FILTER_BITMAP_FLAG)
        paint.color = 0xFF1C1C24.toInt()
        canvas.drawOval(RectF(0f, 0f, SIZE.toFloat(), SIZE.toFloat()), paint)
        paint.xfermode = PorterDuffXfermode(PorterDuff.Mode.SRC_ATOP)
        canvas.drawBitmap(src, Rect(left, 0, left + side, side), Rect(0, 0, SIZE, SIZE), paint)
        return out
    }
}
