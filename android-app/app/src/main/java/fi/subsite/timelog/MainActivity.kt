package fi.subsite.timelog

import android.Manifest
import android.app.Activity
import android.app.Notification
import android.app.NotificationManager
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.view.WindowInsets
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast
import java.io.IOException
import kotlin.concurrent.thread

private const val RECENT_COUNT = 5

class MainActivity : Activity() {
    private lateinit var settings: AppSettings
    private val ticker = Handler(Looper.getMainLooper())
    private val tick = object : Runnable {
        override fun run() {
            showElapsed()
            ticker.postDelayed(this, 1000)
        }
    }
    private val listener: (TimerStatus) -> Unit = { render(it) }

    private val status by lazy { findViewById<TextView>(R.id.status) }
    private val description by lazy { findViewById<TextView>(R.id.description) }
    private val project by lazy { findViewById<TextView>(R.id.project) }
    private val elapsed by lazy { findViewById<TextView>(R.id.elapsed) }
    private val buttons by lazy { findViewById<View>(R.id.buttons) }
    private val pause by lazy { findViewById<Button>(R.id.pause) }
    private val address by lazy { findViewById<EditText>(R.id.address) }
    private val apiKey by lazy { findViewById<EditText>(R.id.api_key) }
    private val recentTitle by lazy { findViewById<View>(R.id.recent_title) }
    private val recentList by lazy { findViewById<LinearLayout>(R.id.recent_list) }

    /** What the recent list was last loaded for; a different timer reloads it. */
    private var recentFor: Any? = Unit

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        settings = AppSettings(this)

        // Android 15+ draws apps edge to edge: keep the content clear of the system bars.
        val root = findViewById<View>(R.id.root)
        root.setOnApplyWindowInsetsListener { view, insets ->
            val bars = if (Build.VERSION.SDK_INT >= 30) {
                insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.ime())
            } else null
            if (bars != null) view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }

        address.setText(settings.address)
        apiKey.setText(settings.apiKey)
        findViewById<Button>(R.id.save).setOnClickListener { save() }
        pause.setOnClickListener {
            val entry = TimerStore.status.entry ?: return@setOnClickListener
            TimerActions.run(applicationContext, if (entry.running) "pause" else "resume")
        }
        findViewById<Button>(R.id.stop).setOnClickListener { TimerActions.run(applicationContext, "stop") }
        val callStyle = findViewById<Switch>(R.id.call_style)
        callStyle.isChecked = settings.callStyle
        callStyle.visibility = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) View.VISIBLE else View.GONE
        callStyle.setOnCheckedChangeListener { _, checked ->
            settings.callStyle = checked
            TimerStore.update { it } // the service redraws the notification
        }
        findViewById<Button>(R.id.battery).setOnClickListener {
            startActivity(
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")),
            )
        }
        findViewById<Button>(R.id.live_updates).setOnClickListener {
            startActivity(
                Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, packageName),
            )
        }

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        if (settings.configured) TimerService.start(this)
    }

    override fun onResume() {
        super.onResume()
        TimerStore.listen(listener)
        ticker.post(tick)
        loadRecent()
        val power = getSystemService(PowerManager::class.java)
        findViewById<View>(R.id.battery).visibility =
            if (power.isIgnoringBatteryOptimizations(packageName)) View.GONE else View.VISIBLE
        val promoted = Build.VERSION.SDK_INT < Build.VERSION_CODES.BAKLAVA ||
            getSystemService(NotificationManager::class.java).canPostPromotedNotifications()
        findViewById<View>(R.id.live_updates).visibility = if (promoted) View.GONE else View.VISIBLE
    }

    override fun onPause() {
        TimerStore.unlisten(listener)
        ticker.removeCallbacks(tick)
        super.onPause()
    }

    private fun save() {
        val newAddress = address.text.toString().trim().trimEnd('/')
        if (newAddress.isNotEmpty() && !newAddress.startsWith("https://") && !newAddress.startsWith("http://")) {
            Toast.makeText(this, "The address must start with https://", Toast.LENGTH_LONG).show()
            return
        }
        val before = settings.address to settings.apiKey
        settings.address = newAddress
        settings.apiKey = apiKey.text.toString()
        address.setText(settings.address)
        if (settings.configured) {
            TimerService.start(this, reconnect = before != (settings.address to settings.apiKey))
            loadRecent()
            Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show()
        } else {
            TimerService.stop(this)
            Toast.makeText(this, "Enter the address and an API key", Toast.LENGTH_LONG).show()
        }
    }

    private fun render(s: TimerStatus) {
        val entry = s.entry
        status.text = when {
            !settings.configured -> "Enter the address and API key below."
            s.problem != null -> s.problem
            !s.connected -> "Connecting…"
            entry == null -> "No timer running"
            entry.running -> "Running"
            else -> "Paused"
        }
        val visible = if (entry == null) View.GONE else View.VISIBLE
        description.visibility = visible
        project.visibility = visible
        elapsed.visibility = visible
        buttons.visibility = visible
        if (entry != null) {
            description.text = entry.description.ifBlank { "(no description)" }
            project.text = listOf(entry.project, entry.customer).filter { it.isNotBlank() }.joinToString(" · ")
            pause.text = if (entry.running) "Pause" else "Resume"
        }
        showElapsed()
        val key = entry?.let { listOf(it.description, it.project, it.customer) }
        if (key != recentFor) loadRecent()
        // Re-read once the service has posted the changed notification.
        ticker.postDelayed(::showDiagnostics, 500)
    }

    /** Version and why the status bar chip shows or not, for troubleshooting. */
    private fun showDiagnostics() {
        val info = packageManager.getPackageInfo(packageName, 0)
        val lines = mutableListOf(
            "Version ${info.versionName} · Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})" +
                " · ${Build.MANUFACTURER} ${Build.MODEL}",
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.BAKLAVA) {
            val manager = getSystemService(NotificationManager::class.java)
            lines += "Live updates allowed: ${manager.canPostPromotedNotifications()}"
            val active = manager.activeNotifications.firstOrNull { it.id == TimerService.NOTIFICATION_ID }
            if (active != null) {
                val n = active.notification
                lines += "Channel: ${n.channelId} · promotable: ${n.hasPromotableCharacteristics()}" +
                    " · promoted: ${n.flags and Notification.FLAG_PROMOTED_ONGOING != 0}"
            }
        }
        findViewById<TextView>(R.id.version).text = lines.joinToString("\n")
    }

    // --- Recent tasks ---

    private fun loadRecent() {
        recentFor = TimerStore.status.entry?.let { listOf(it.description, it.project, it.customer) }
        if (!settings.configured) return showRecent(emptyList())
        val api = TimelogApi(settings.address, settings.apiKey)
        thread(name = "timelog-recent") {
            val tasks = try {
                api.recent(RECENT_COUNT)
            } catch (e: IOException) {
                null
            }
            runOnUiThread { if (tasks != null) showRecent(tasks) }
        }
    }

    private fun showRecent(tasks: List<RecentTask>) {
        recentTitle.visibility = if (tasks.isEmpty()) View.GONE else View.VISIBLE
        recentList.removeAllViews()
        val gap = (8 * resources.displayMetrics.density).toInt()
        for (task in tasks) {
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setBackgroundResource(R.drawable.row)
                setPadding(gap * 2, gap + gap / 2, gap * 2, gap + gap / 2)
                isClickable = true
                contentDescription = "${getString(R.string.start_task)} ${task.description}"
                setOnClickListener {
                    TimerActions.start(applicationContext, task)
                    Toast.makeText(this@MainActivity, "Starting", Toast.LENGTH_SHORT).show()
                }
            }
            row.addView(TextView(this).apply {
                text = task.description.ifBlank { "(no description)" }
                setTextAppearance(android.R.style.TextAppearance_Material_Body2)
            })
            row.addView(TextView(this).apply {
                text = listOf(task.project, task.customer).filter { it.isNotBlank() }.joinToString(" · ")
                setTextAppearance(android.R.style.TextAppearance_Material_Caption)
            })
            val params = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = gap }
            recentList.addView(row, params)
        }
    }

    private fun showElapsed() {
        val entry = TimerStore.status.entry ?: return
        elapsed.text = formatElapsed(entry.elapsedNow())
    }
}
