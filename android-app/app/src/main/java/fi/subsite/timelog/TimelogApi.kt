package fi.subsite.timelog

import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

class ApiException(message: String, val status: Int = 0) : IOException(message)

/** A recently used description and project, from /api/v1/recent. */
data class RecentTask(
    val description: String,
    val projectId: Long,
    val project: String,
    val customer: String,
    val billable: Boolean,
    /** Time on this task today and this month, in the time zone sent with the request. */
    val todaySeconds: Long,
    val monthSeconds: Long,
)

/** The public API (/api/v1/), authenticated with the API key. */
class TimelogApi(private val address: String, private val apiKey: String) {

    fun open(path: String, method: String = "GET"): HttpURLConnection {
        val conn = URL(address + path).openConnection() as HttpURLConnection
        conn.requestMethod = method
        conn.setRequestProperty("Authorization", "Bearer $apiKey")
        conn.setRequestProperty("User-Agent", "timelog-android")
        conn.connectTimeout = 15_000
        conn.readTimeout = 20_000
        return conn
    }

    /** Sends a request and returns the answer's body, or throws ApiException for errors. */
    private fun request(path: String, method: String, body: JSONObject? = null): String {
        val conn = open(path, method)
        try {
            if (method == "POST") {
                val bytes = (body?.toString() ?: "").toByteArray()
                conn.doOutput = true
                if (body != null) conn.setRequestProperty("Content-Type", "application/json")
                conn.setFixedLengthStreamingMode(bytes.size)
                conn.outputStream.use { it.write(bytes) }
            }
            val status = conn.responseCode
            val text = (if (status < 400) conn.inputStream else conn.errorStream)
                ?.bufferedReader()?.use { it.readText() }.orEmpty()
            if (status >= 400) throw ApiException(errorMessage(status, text), status)
            return text
        } finally {
            conn.disconnect()
        }
    }

    /** POSTs a timer action (pause, resume, stop) and returns the timer answer. */
    fun timerAction(action: String): JSONObject = JSONObject(request("/api/v1/timer/$action", "POST"))

    /** Starts a new timer for a recent task, stopping the current one. */
    fun start(task: RecentTask): JSONObject {
        val body = JSONObject()
            .put("project_id", task.projectId)
            .put("description", task.description)
            .put("billable", task.billable)
        return JSONObject(request("/api/v1/timer/start", "POST", body))
    }

    fun recent(limit: Int, timeZone: String): List<RecentTask> {
        val tz = URLEncoder.encode(timeZone, "UTF-8")
        val list = JSONArray(request("/api/v1/recent?limit=$limit&tz=$tz", "GET"))
        return (0 until list.length()).map { i ->
            val o = list.getJSONObject(i)
            RecentTask(
                description = o.optString("description"),
                projectId = o.getLong("project_id"),
                project = o.optString("project"),
                customer = o.optString("customer"),
                billable = o.optBoolean("billable", true),
                todaySeconds = o.optLong("today_seconds"),
                monthSeconds = o.optLong("month_seconds"),
            )
        }
    }

    companion object {
        fun errorMessage(status: Int, body: String): String {
            if (status == 401) return "The API key is wrong or was removed."
            if (status == 404) return "This Timelog does not support that yet; update the server."
            val message = runCatching { JSONObject(body).optString("error") }.getOrNull()
            return if (message.isNullOrBlank()) "Timelog answered $status." else message
        }
    }
}
