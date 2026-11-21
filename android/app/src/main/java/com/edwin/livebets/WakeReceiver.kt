package com.edwin.livebets

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Pre-game alarm: wake the service (or restart it if Android killed it). */
class WakeReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val svc = BetService.running
        if (svc != null) {
            svc.wakeAndPoll()
        } else {
            // Starting a foreground service from an exact alarm is allowed
            try { BetService.start(context) } catch (e: Exception) { }
        }
    }
}
