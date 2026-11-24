package fi.subsite.timelog

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import java.io.IOException
import java.util.TimeZone
import kotlin.concurrent.thread

private const val RECENT_COUNT = 5

/** The timer card and the recent tasks; the settings are in SettingsActivity. */
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
    private val recentTitle by lazy { findViewById<View>(R.id.recent_title) }
    private val recentList by lazy { findViewById<LinearLayout>(R.id.recent_list) }

    /** What the recent list was last loaded for; a different timer reloads it. */
    private var recentFor: Any? = Unit

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        settings = AppSettings(this)
        findViewById<View>(R.id.root).fitSystemBars()

        findViewById<View>(R.id.settings).setOnClickListener { openSettings() }
        pause.setOnClickListener {
            val entry = TimerStore.status.entry ?: return@setOnClickListener
            TimerActions.run(applicationContext, if (entry.running) "pause" else "resume")
        }
        findViewById<Button>(R.id.stop).setOnClickListener { TimerActions.run(applicationContext, "stop") }

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        if (settings.configured) TimerService.start(this)
        else if (savedInstanceState == null) openSettings()
    }

    override fun onResume() {
        super.onResume()
        TimerStore.listen(listener)
        ticker.post(tick)
        loadRecent()
    }

    override fun onPause() {
        TimerStore.unlisten(listener)
        ticker.removeCallbacks(tick)
        super.onPause()
    }

    private fun openSettings() = startActivity(Intent(this, SettingsActivity::class.java))

    private fun render(s: TimerStatus) {
        val entry = s.entry
        status.text = when {
            !settings.configured -> "Enter the address and API key in the settings (top right)."
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
        val key = entry?.let { listOf(it.state, it.description, it.project, it.customer) }
        if (key != recentFor) loadRecent()
    }

    // --- Recent tasks ---

    private fun loadRecent() {
        recentFor = TimerStore.status.entry?.let { listOf(it.state, it.description, it.project, it.customer) }
        if (!settings.configured) return showRecent(emptyList())
        val api = TimelogApi(settings.address, settings.apiKey)
        thread(name = "timelog-recent") {
            val tasks = try {
                api.recent(RECENT_COUNT, TimeZone.getDefault().id)
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
            row.addView(TextView(this).apply {
                text = "Today ${formatHoursMinutes(task.todaySeconds)}" +
                    " · This month ${formatHoursMinutes(task.monthSeconds)}"
                setTextAppearance(android.R.style.TextAppearance_Material_Caption)
                fontFeatureSettings = "tnum"
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
