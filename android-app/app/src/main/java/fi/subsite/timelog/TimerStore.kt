package fi.subsite.timelog

import android.content.Context
import android.os.Handler
import android.os.Looper
import org.json.JSONObject

/** The running or paused entry, as /api/v1/timer reports it. */
data class Entry(
    val state: String,
    val description: String,
    val project: String,
    val customer: String,
    val billable: Boolean,
    val elapsedSeconds: Long,
    /** Wall-clock milliseconds when elapsedSeconds was true. */
    val receivedAt: Long,
) {
    val running get() = state == "running"

    /** Elapsed seconds now, counting on locally while the timer runs. */
    fun elapsedNow(now: Long = System.currentTimeMillis()): Long =
        if (running) elapsedSeconds + maxOf(0L, (now - receivedAt) / 1000) else elapsedSeconds

    /** The wall-clock time the timer would have started at, for chronometers. */
    fun base(): Long = receivedAt - elapsedSeconds * 1000

    companion object {
        /** The entry in a timer answer, or null when no timer is running or paused. */
        fun fromTimer(json: JSONObject): Entry? {
            val e = json.optJSONObject("entry") ?: return null
            return Entry(
                state = e.getString("state"),
                description = e.optString("description"),
                project = e.optString("project"),
                customer = e.optString("customer"),
                billable = e.optBoolean("billable"),
                elapsedSeconds = e.optLong("elapsed_seconds"),
                receivedAt = System.currentTimeMillis(),
            )
        }
    }
}

/** What the service knows; the activity and the notification both draw from it. */
data class TimerStatus(
    val connected: Boolean = false,
    val entry: Entry? = null,
    val problem: String? = null,
)

object TimerStore {
    private val main = Handler(Looper.getMainLooper())
    private val listeners = mutableSetOf<(TimerStatus) -> Unit>()

    @Volatile
    var status = TimerStatus()
        private set

    fun update(change: (TimerStatus) -> TimerStatus) {
        synchronized(this) { status = change(status) }
        main.post { listeners.toList().forEach { it(status) } }
    }

    fun listen(listener: (TimerStatus) -> Unit) {
        listeners += listener
        listener(status)
    }

    fun unlisten(listener: (TimerStatus) -> Unit) {
        listeners -= listener
    }
}

fun formatElapsed(seconds: Long): String {
    val s = maxOf(0L, seconds)
    return "%d:%02d:%02d".format(s / 3600, s / 60 % 60, s % 60)
}

/** Hours and minutes, like 12:05, for totals. */
fun formatHoursMinutes(seconds: Long): String {
    val m = maxOf(0L, seconds) / 60
    return "%d:%02d".format(m / 60, m % 60)
}

/** The address and API key, kept in the app's private storage. */
class AppSettings(context: Context) {
    private val prefs = context.getSharedPreferences("settings", Context.MODE_PRIVATE)

    var address: String
        get() = prefs.getString("address", DEFAULT_ADDRESS) ?: DEFAULT_ADDRESS
        set(value) = prefs.edit().putString("address", value.trim().trimEnd('/')).apply()

    var apiKey: String
        get() = prefs.getString("api_key", "") ?: ""
        set(value) = prefs.edit().putString("api_key", value.trim()).apply()

    /** Show the running timer as an ongoing call, whose status bar pill every phone ticks. */
    var callStyle: Boolean
        get() = prefs.getBoolean("call_style", false)
        set(value) = prefs.edit().putBoolean("call_style", value).apply()

    val configured get() = address.isNotBlank() && apiKey.isNotBlank()

    companion object {
        const val DEFAULT_ADDRESS = "https://timelog.subsite.fi"
    }
}
