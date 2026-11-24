package fi.subsite.timelog

import android.os.Build
import android.view.View
import android.view.WindowInsets

/** Android 15+ draws apps edge to edge: keeps the view's content clear of the system bars. */
fun View.fitSystemBars() {
    setOnApplyWindowInsetsListener { view, insets ->
        if (Build.VERSION.SDK_INT >= 30) {
            val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.ime())
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
        }
        insets
    }
}
