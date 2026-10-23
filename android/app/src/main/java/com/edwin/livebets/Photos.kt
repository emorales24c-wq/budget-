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
    private val cache = mutableMapOf<String, Bitmap>()
    private val failed = mutableSetOf<String>()

    fun get(id: String): Bitmap? {
        if (id.isEmpty() || id in failed) return null
        cache[id]?.let { return it }
        return try {
            val c = URL("https://a.espncdn.com/i/headshots/nba/players/full/$id.png").openConnection() as HttpURLConnection
            c.connectTimeout = 8000
            c.readTimeout = 8000
            val src = c.inputStream.use { BitmapFactory.decodeStream(it) } ?: throw RuntimeException("decode")
            c.disconnect()
            circle(src).also { cache[id] = it }
        } catch (e: Exception) {
            failed.add(id)
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
