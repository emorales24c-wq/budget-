package com.edwin.livebets

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

data class Leg(
    val game: String,
    val gameName: String,
    val player: String,
    val stat: String,
    val side: String,
    val line: Double,
)

/** A single bet is a bet with one leg and parlay = false. */
data class Bet(
    val id: Long,
    val parlay: Boolean,
    val legs: List<Leg>,
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
            val parlay = o.optBoolean("parlay")
            val legs = if (parlay) {
                val l = o.optJSONArray("legs") ?: JSONArray()
                (0 until l.length()).map { leg(l.getJSONObject(it)) }
            } else listOf(leg(o))
            Bet(
                id = o.optLong("id"),
                parlay = parlay,
                legs = legs,
                stake = o.optDouble("stake", 0.0),
                odds = o.optString("odds", ""),
            )
        }
    }

    private fun leg(o: JSONObject) = Leg(
        game = o.optString("game"),
        gameName = o.optString("gameName"),
        player = o.optString("player"),
        stat = o.optString("stat", "PTS"),
        side = o.optString("side", "over"),
        line = o.optDouble("line", 0.0),
    )
}
