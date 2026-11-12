package com.edwin.livebets

import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.text.Normalizer

data class LiveGame(
    val start: Long,            // tip-off, epoch ms (0 if unknown)
    val state: String,          // pre | in | post
    val detail: String,         // "Q3 5:12", "Final", "10/23 - 4:00 PM EDT"
    val score: String,
    val players: Map<String, PlayerLine>,
)

data class PlayerLine(val name: String, val id: String, val stats: Map<String, Int>)

object Espn {
    private const val API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"

    // Boxscore column aliases: older responses use labels, newer ones keys
    private val COLS = mapOf(
        "PTS" to listOf("PTS", "points"),
        "REB" to listOf("REB", "rebounds"),
        "AST" to listOf("AST", "assists"),
        "STL" to listOf("STL", "steals"),
        "BLK" to listOf("BLK", "blocks"),
        "3PM" to listOf("3PT", "threePointFieldGoalsMade-threePointFieldGoalsAttempted"),
    )

    val STATS = mapOf(
        "PTS" to listOf("PTS"), "REB" to listOf("REB"), "AST" to listOf("AST"),
        "3PM" to listOf("3PM"), "STL" to listOf("STL"), "BLK" to listOf("BLK"),
        "PRA" to listOf("PTS", "REB", "AST"), "PR" to listOf("PTS", "REB"),
        "PA" to listOf("PTS", "AST"), "RA" to listOf("REB", "AST"),
    )

    fun norm(s: String): String =
        Normalizer.normalize(s, Normalizer.Form.NFD)
            .replace(Regex("\\p{M}"), "").lowercase()
            .replace(Regex("[^a-z ]"), "").trim()

    private fun get(url: String): JSONObject {
        val c = URL(url).openConnection() as HttpURLConnection
        c.connectTimeout = 10000
        c.readTimeout = 10000
        c.useCaches = false
        try {
            if (c.responseCode != 200) throw RuntimeException("HTTP ${c.responseCode}")
            return JSONObject(c.inputStream.bufferedReader().readText())
        } finally {
            c.disconnect()
        }
    }

    private fun JSONArray?.strings(): List<String> =
        if (this == null) emptyList() else (0 until length()).map { optString(it) }

    fun summary(eventId: String): LiveGame {
        val d = get("$API/summary?event=$eventId")
        val comp = d.getJSONObject("header").getJSONArray("competitions").getJSONObject(0)
        val type = comp.getJSONObject("status").getJSONObject("type")
        val teams = comp.getJSONArray("competitors")
        val score = (0 until teams.length()).joinToString(" - ") {
            val t = teams.getJSONObject(it)
            "${t.getJSONObject("team").optString("abbreviation")} ${t.optString("score", "0")}"
        }

        val players = mutableMapOf<String, PlayerLine>()
        val box = d.optJSONObject("boxscore")?.optJSONArray("players") ?: JSONArray()
        for (ti in 0 until box.length()) {
            val groups = box.getJSONObject(ti).optJSONArray("statistics") ?: continue
            for (gi in 0 until groups.length()) {
                val grp = groups.getJSONObject(gi)
                val labels = (grp.optJSONArray("labels") ?: grp.optJSONArray("names")).strings()
                val keys = grp.optJSONArray("keys").strings()
                fun idx(name: String): Int {
                    for (alias in COLS.getValue(name)) {
                        var i = labels.indexOf(alias)
                        if (i < 0) i = keys.indexOf(alias)
                        if (i >= 0) return i
                    }
                    return -1
                }
                val athletes = grp.optJSONArray("athletes") ?: continue
                for (ai in 0 until athletes.length()) {
                    val a = athletes.getJSONObject(ai)
                    val stats = a.optJSONArray("stats")
                    if (stats == null || stats.length() == 0) continue  // DNP
                    val vals = COLS.keys.associateWith { k ->
                        val i = idx(k)
                        if (i < 0) 0 else stats.optString(i).split("-")[0].toIntOrNull() ?: 0
                    }
                    val athlete = a.getJSONObject("athlete")
                    val name = athlete.optString("displayName")
                    players[norm(name)] = PlayerLine(name, athlete.optString("id"), vals)
                }
            }
        }
        return LiveGame(parseTime(comp.optString("date")), type.optString("state"), type.optString("shortDetail"), score, players)
    }

    // ESPN dates look like "2026-10-23T20:00Z"
    private fun parseTime(s: String): Long = try {
        java.time.OffsetDateTime.parse(s, java.time.format.DateTimeFormatter.ISO_OFFSET_DATE_TIME).toInstant().toEpochMilli()
    } catch (e: Exception) {
        try { java.time.Instant.parse(s).toEpochMilli() } catch (e2: Exception) { 0L }
    }

    fun findPlayer(g: LiveGame, query: String): PlayerLine? {
        val n = norm(query)
        g.players[n]?.let { return it }
        val last = n.split(" ").last()
        return g.players.entries.firstOrNull { (k, _) -> k.contains(n) || k.split(" ").last() == last }?.value
    }
}
