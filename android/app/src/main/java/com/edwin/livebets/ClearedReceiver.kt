package com.edwin.livebets

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Fired when an open bet's notification is cleared; the service shows it again. */
class ClearedReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        BetService.running?.repostSoon()
    }
}
