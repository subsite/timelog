package fi.subsite.timelog

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.widget.Toast
import org.json.JSONObject
import java.io.IOException
import kotlin.concurrent.thread

/** Pause, resume and stop from the notification's buttons. */
class ActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = when (intent.action) {
            TimerService.ACTION_PAUSE -> "pause"
            TimerService.ACTION_RESUME -> "resume"
            TimerService.ACTION_STOP -> "stop"
            else -> return
        }
        val pending = goAsync()
        TimerActions.run(context.applicationContext, action) { pending.finish() }
    }
}

object TimerActions {
    private val main = Handler(Looper.getMainLooper())

    /** Sends a timer action; the stream would report the change too, but this is quicker. */
    fun run(context: Context, action: String, done: () -> Unit = {}) =
        send(context, action, done) { it.timerAction(action) }

    /** Starts a recent task, stopping the current timer. */
    fun start(context: Context, task: RecentTask) = send(context, "start", {}) { it.start(task) }

    private fun send(context: Context, name: String, done: () -> Unit, call: (TimelogApi) -> JSONObject) {
        val settings = AppSettings(context)
        thread(name = "timelog-$name") {
            try {
                val json = call(TimelogApi(settings.address, settings.apiKey))
                TimerStore.update { it.copy(entry = Entry.fromTimer(json)) }
            } catch (e: IOException) {
                val message = e.message ?: "Cannot reach Timelog."
                main.post { Toast.makeText(context, message, Toast.LENGTH_LONG).show() }
            } finally {
                done()
            }
        }
    }
}
