package fi.subsite.timelog

import android.app.Activity
import android.app.Notification
import android.app.NotificationManager
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast

/** The address and API key, the call style, background and live update permissions, diagnostics. */
class SettingsActivity : Activity() {
    private lateinit var settings: AppSettings
    private val main = Handler(Looper.getMainLooper())
    private val diagnose = Runnable { showDiagnostics() }
    // Re-read once the service has posted the changed notification.
    private val listener: (TimerStatus) -> Unit = {
        main.removeCallbacks(diagnose)
        main.postDelayed(diagnose, 500)
    }

    private val address by lazy { findViewById<EditText>(R.id.address) }
    private val apiKey by lazy { findViewById<EditText>(R.id.api_key) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_settings)
        settings = AppSettings(this)
        findViewById<View>(R.id.root).fitSystemBars()

        address.setText(settings.address)
        apiKey.setText(settings.apiKey)
        findViewById<Button>(R.id.save).setOnClickListener { save() }
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
    }

    override fun onResume() {
        super.onResume()
        TimerStore.listen(listener)
        val power = getSystemService(PowerManager::class.java)
        findViewById<View>(R.id.battery).visibility =
            if (power.isIgnoringBatteryOptimizations(packageName)) View.GONE else View.VISIBLE
        val promoted = Build.VERSION.SDK_INT < Build.VERSION_CODES.BAKLAVA ||
            getSystemService(NotificationManager::class.java).canPostPromotedNotifications()
        findViewById<View>(R.id.live_updates).visibility = if (promoted) View.GONE else View.VISIBLE
    }

    override fun onPause() {
        TimerStore.unlisten(listener)
        main.removeCallbacks(diagnose)
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
            Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show()
            finish()
        } else {
            TimerService.stop(this)
            Toast.makeText(this, "Enter the address and an API key", Toast.LENGTH_LONG).show()
        }
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
}
