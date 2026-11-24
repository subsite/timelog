package fi.subsite.timelog

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Starts listening for the timer after a reboot or an app update. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED && intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        if (AppSettings(context).configured) TimerService.start(context)
    }
}
