package fi.subsite.timelog

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Person
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.drawable.Icon
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.content.BroadcastReceiver
import android.content.IntentFilter
import android.util.Log
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection

/**
 * Keeps /api/v1/timer/stream open for as long as the app is set up, and shows the timer as an
 * ongoing notification: a Live Update with a status bar chip on Android 16+, a plain ongoing
 * notification with a chronometer before that.
 */
class TimerService : Service() {
    private var worker: Thread? = null
    private val lock = Object()

    @Volatile private var connection: HttpURLConnection? = null
    @Volatile private var generation = 0
    @Volatile private var delay = 1
    private var lastNetwork: Network? = null
    private var notifier: ((TimerStatus) -> Unit)? = null

    private val networkCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) {
            // A new network (Wi-Fi to mobile and back) leaves the old socket hanging: reconnect.
            if (lastNetwork != null && network != lastNetwork) reconnect() else wake()
            lastNetwork = network
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannels(this)
        getSystemService(ConnectivityManager::class.java).registerDefaultNetworkCallback(networkCallback)
        val listener: (TimerStatus) -> Unit = { status ->
            getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, build(status))
            scheduleTick()
        }
        notifier = listener
        TimerStore.listen(listener)
        registerReceiver(screenReceiver, IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_SCREEN_OFF)
        })
    }

    // --- The status bar chip's clock ---

    private val main = Handler(Looper.getMainLooper())
    private val tick = Runnable {
        getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, build(TimerStore.status))
        scheduleTick()
    }

    private val screenReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action == Intent.ACTION_SCREEN_ON) tick.run() else main.removeCallbacks(tick)
        }
    }

    /** Refreshes the chip's time on the next whole second, while it runs and can be seen. */
    private fun scheduleTick() {
        main.removeCallbacks(tick)
        // Before Android 16 there is no chip, and the call pill's clock ticks itself.
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.BAKLAVA || callStyle()) return
        val entry = TimerStore.status.entry ?: return
        if (!entry.running || !getSystemService(PowerManager::class.java).isInteractive) return
        val sinceStart = System.currentTimeMillis() - entry.base()
        main.postDelayed(tick, 1000 - Math.floorMod(sinceStart, 1000L) + 20)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val notification = build(TimerStore.status)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
        if (!AppSettings(this).configured) {
            stopSelf()
            return START_NOT_STICKY
        }
        if (intent?.action == ACTION_RECONNECT) reconnect()
        if (worker == null) {
            worker = Thread(::run, "timelog-stream").also { it.start() }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        notifier?.let { TimerStore.unlisten(it) }
        main.removeCallbacks(tick)
        runCatching { unregisterReceiver(screenReceiver) }
        runCatching {
            getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(networkCallback)
        }
        worker?.interrupt()
        worker = null
        connection?.disconnect()
        TimerStore.update { TimerStatus() }
        super.onDestroy()
    }

    // --- The stream ---

    private fun run() {
        while (!Thread.currentThread().isInterrupted) {
            val started = generation
            val problem = try {
                val settings = AppSettings(this)
                stream(TimelogApi(settings.address, settings.apiKey))
                "Timelog closed the connection."
            } catch (e: ApiException) {
                // A wrong key will not fix itself soon; saving new settings reconnects at once.
                if (e.status == 401) delay = 300
                Log.w(TAG, "Stream refused", e)
                e.message
            } catch (e: IOException) {
                Log.w(TAG, "Stream failed", e)
                "Cannot reach Timelog: ${e.message ?: e.javaClass.simpleName}"
            } catch (e: Exception) {
                Log.w(TAG, "Stream failed", e)
                "Unexpected answer from Timelog: ${e.message}"
            }
            if (Thread.currentThread().isInterrupted) return
            if (started != generation) {
                // We closed the connection ourselves (new settings, new network): not a problem.
                TimerStore.update { it.copy(connected = false, problem = null) }
                continue
            }
            // The entry stays so the notification keeps counting while we reconnect.
            TimerStore.update { it.copy(connected = false, problem = problem) }
            try {
                synchronized(lock) { lock.wait(delay * 1000L) }
            } catch (e: InterruptedException) {
                return
            }
            delay = minOf(delay * 2, maxOf(delay, 60))
        }
    }

    private fun stream(api: TimelogApi) {
        val conn = api.open("/api/v1/timer/stream")
        conn.setRequestProperty("Accept", "text/event-stream")
        conn.readTimeout = 70_000 // the server sends a keep-alive every 25 seconds
        connection = conn
        try {
            val status = conn.responseCode
            if (status != 200) {
                val body = conn.errorStream?.bufferedReader()?.use { it.readText() }.orEmpty()
                throw ApiException(TimelogApi.errorMessage(status, body), status)
            }
            delay = 1
            TimerStore.update { it.copy(connected = true, problem = null) }
            conn.inputStream.bufferedReader().use { reader ->
                var event = "message"
                val data = StringBuilder()
                while (true) {
                    val line = reader.readLine() ?: return
                    when {
                        line.isEmpty() -> {
                            if (event == "timer" && data.isNotEmpty()) {
                                val entry = Entry.fromTimer(JSONObject(data.toString()))
                                TimerStore.update { it.copy(entry = entry) }
                            }
                            event = "message"
                            data.clear()
                        }
                        line.startsWith(":") -> Unit
                        line.startsWith("event:") -> event = line.substring(6).trim()
                        line.startsWith("data:") -> {
                            if (data.isNotEmpty()) data.append('\n')
                            data.append(line.substring(5).removePrefix(" "))
                        }
                    }
                }
            }
        } finally {
            connection = null
            conn.disconnect()
        }
    }

    private fun reconnect() {
        Log.i(TAG, "Reconnecting")
        generation++
        delay = 1
        connection?.disconnect()
        wake()
    }

    private fun wake() {
        synchronized(lock) { lock.notifyAll() }
    }

    // --- The notification ---

    private fun build(status: TimerStatus): Notification {
        val entry = status.entry
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )
        val builder = Notification.Builder(this, if (entry == null) CHANNEL_IDLE else CHANNEL_TIMER)
            .setContentIntent(open)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSmallIcon(R.drawable.ic_timer)

        if (entry == null) {
            return builder
                .setContentTitle(getString(R.string.app_name))
                .setContentText(status.problem ?: if (status.connected) "No timer running" else "Connecting…")
                .setShowWhen(false)
                .build()
        }

        val title = entry.description.ifBlank { "(no description)" }
        val where = listOf(entry.project, entry.customer).filter { it.isNotBlank() }.joinToString(" · ")
        builder.setContentTitle(title)
        if (status.problem != null) builder.setSubText("Offline")

        if (entry.running && callStyle()) {
            // An ongoing call: every status bar (Samsung's too) shows a pill with its clock.
            // Its red "hang up" button stops the timer.
            val person = Person.Builder().setName(title).setImportant(true).build()
            return builder
                .setStyle(Notification.CallStyle.forOngoingCall(person, pending(ACTION_STOP)))
                .setCategory(Notification.CATEGORY_CALL)
                .setContentText(where)
                .setWhen(entry.base())
                .setShowWhen(true)
                .setUsesChronometer(true)
                .addAction(action(ACTION_PAUSE, R.drawable.ic_pause, "Pause"))
                .build()
        }

        builder.setCategory(if (Build.VERSION.SDK_INT >= 30) Notification.CATEGORY_STOPWATCH else Notification.CATEGORY_PROGRESS)
            .addAction(
                if (entry.running) action(ACTION_PAUSE, R.drawable.ic_pause, "Pause")
                else action(ACTION_RESUME, R.drawable.ic_play, "Resume"),
            )
            .addAction(action(ACTION_STOP, R.drawable.ic_stop, "Stop"))

        if (entry.running && Build.VERSION.SDK_INT >= Build.VERSION_CODES.BAKLAVA) {
            // tick() re-posts every second here, so the time can be text: Samsung crams the
            // chronometer right against the title.
            val elapsed = formatElapsed(entry.elapsedNow())
            builder.setContentText(if (where.isEmpty()) elapsed else "$elapsed · $where")
                .setShowWhen(false)
        } else if (entry.running) {
            builder.setContentText(where)
                .setWhen(entry.base())
                .setShowWhen(true)
                .setUsesChronometer(true)
        } else {
            val elapsed = formatElapsed(entry.elapsedSeconds)
            builder.setContentText(if (where.isEmpty()) "Paused · $elapsed" else "Paused · $elapsed · $where")
                .setSmallIcon(R.drawable.ic_pause)
                .setShowWhen(false)
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.BAKLAVA) {
            // A Live Update: the status bar chip shows this text next to the icon. Some status
            // bars ignore the chronometer, so the text is the time itself, which tick()
            // refreshes every second while the timer runs and the screen is on.
            // setRequestPromotedOngoing() is only public from API 36.1; it sets this extra.
            builder.addExtras(android.os.Bundle().apply { putBoolean(EXTRA_REQUEST_PROMOTED, true) })
            builder.setShortCriticalText(formatElapsed(entry.elapsedNow()))
        }
        return builder.build()
    }

    /** Call style needs Android 12. */
    private fun callStyle() = Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && AppSettings(this).callStyle

    private fun pending(name: String): PendingIntent {
        val intent = Intent(this, ActionReceiver::class.java).setAction(name)
        return PendingIntent.getBroadcast(
            this, name.hashCode(), intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
    }

    private fun action(name: String, icon: Int, label: String): Notification.Action =
        Notification.Action.Builder(Icon.createWithResource(this, icon), label, pending(name)).build()

    companion object {
        const val NOTIFICATION_ID = 1
        const val TAG = "Timelog"
        const val EXTRA_REQUEST_PROMOTED = "android.requestPromotedOngoing"
        const val CHANNEL_TIMER = "timer"
        const val CHANNEL_IDLE = "idle"
        const val ACTION_RECONNECT = "fi.subsite.timelog.RECONNECT"
        const val ACTION_PAUSE = "fi.subsite.timelog.PAUSE"
        const val ACTION_RESUME = "fi.subsite.timelog.RESUME"
        const val ACTION_STOP = "fi.subsite.timelog.STOP"

        fun start(context: Context, reconnect: Boolean = false) {
            val intent = Intent(context, TimerService::class.java)
            if (reconnect) intent.action = ACTION_RECONNECT
            context.startForegroundService(intent)
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, TimerService::class.java))
        }

        fun createChannels(context: Context) {
            val manager = context.getSystemService(NotificationManager::class.java)
            val timer = NotificationChannel(CHANNEL_TIMER, "Running timer", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "The running or paused timer, with pause, resume and stop"
                setSound(null, null)
                enableVibration(false)
                setShowBadge(false)
            }
            val idle = NotificationChannel(CHANNEL_IDLE, "No timer running", NotificationManager.IMPORTANCE_MIN).apply {
                description = "Shown while no timer runs and the app waits for one. You can turn this off."
                setShowBadge(false)
            }
            manager.createNotificationChannels(listOf(timer, idle))
        }
    }
}
