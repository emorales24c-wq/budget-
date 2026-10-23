package com.edwin.livebets

import android.content.Context
import org.json.JSONArray

data class Bet(
    val id: Long,
    val game: String,
    val gameName: String,
    val player: String,
    val stat: String,
    val side: String,
    val line: Double,
    val stake: Double,
    val odds: String,
)

object BetStore {
    private const val PREFS = "livebets"
    private const val KEY = "bets"

    fun save(ctx: Context, json: String) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(KEY, json).apply()
    }

    fun load(ctx: Context): List<Bet> {
        val raw = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY, "[]") ?: "[]"
        val arr = try { JSONArray(raw) } catch (e: Exception) { JSONArray() }
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Bet(
                id = o.optLong("id"),
                game = o.optString("game"),
                gameName = o.optString("gameName"),
                player = o.optString("player"),
                stat = o.optString("stat", "PTS"),
                side = o.optString("side", "over"),
                line = o.optDouble("line", 0.0),
                stake = o.optDouble("stake", 0.0),
                odds = o.optString("odds", ""),
            )
        }
    }
}
