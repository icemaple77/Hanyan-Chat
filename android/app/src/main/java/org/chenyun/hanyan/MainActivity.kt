package org.chenyun.hanyan

import android.Manifest
import android.annotation.SuppressLint
import android.content.pm.PackageManager
import android.os.Bundle
import android.text.InputType
import android.view.Menu
import android.view.MenuItem
import android.view.WindowManager
import android.webkit.CookieManager
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

/**
 * Hanyan Chat — WebView 壳
 * =========================
 * 加载自建服务器上的 Hanyan WebUI（/chat 聊天、/call 语音通话），
 * 桥接麦克风权限给页面的 getUserMedia，Cookie 持久化保持登录态。
 *
 * 服务器地址首次启动时输入，之后可在右上角菜单里改。
 * 注意：getUserMedia 要求安全上下文——正式使用请配 https 域名（见项目 README
 * 的 nginx 方案）；http://192.168.x.x 局域网地址在 WebView 里麦克风会被浏览器
 * 内核拒绝，只有 https（或 localhost）可以。
 */
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private var pendingPermissionRequest: PermissionRequest? = null
    private val prefs by lazy { getSharedPreferences("hanyan", MODE_PRIVATE) }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        webView = WebView(this)
        setContentView(webView)

        // 通话时屏幕常亮
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        with(webView.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false // 她的语音回复自动播放
        }
        CookieManager.getInstance().setAcceptCookie(true)

        webView.webViewClient = WebViewClient()
        webView.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: PermissionRequest) {
                runOnUiThread {
                    val wantsMic = request.resources.contains(PermissionRequest.RESOURCE_AUDIO_CAPTURE)
                    when {
                        !wantsMic -> request.deny()
                        hasMicPermission() -> request.grant(
                            arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE)
                        )
                        else -> {
                            pendingPermissionRequest = request
                            ActivityCompat.requestPermissions(
                                this@MainActivity, arrayOf(Manifest.permission.RECORD_AUDIO), REQ_MIC
                            )
                        }
                    }
                }
            }
        }

        // 先把系统级麦克风权限要下来，页面里点"开始通话"就不会卡权限弹窗
        if (!hasMicPermission()) {
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.RECORD_AUDIO), REQ_MIC)
        }

        val url = prefs.getString(KEY_URL, null)
        if (url.isNullOrBlank()) promptForUrl(first = true) else webView.loadUrl(url)
    }

    private fun hasMicPermission() =
        ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_MIC) {
            pendingPermissionRequest?.let { req ->
                if (hasMicPermission()) req.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                else req.deny()
                pendingPermissionRequest = null
            }
        }
    }

    private fun promptForUrl(first: Boolean = false) {
        val input = EditText(this).apply {
            inputType = InputType.TYPE_TEXT_VARIATION_URI
            hint = "https://hanyan.example.com"
            setText(prefs.getString(KEY_URL, ""))
        }
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.server_url))
            .setMessage(getString(R.string.server_url_hint))
            .setView(input)
            .setCancelable(!first)
            .setPositiveButton("确定") { _, _ ->
                var url = input.text.toString().trim()
                if (url.isNotBlank()) {
                    if (!url.startsWith("http://") && !url.startsWith("https://")) url = "https://$url"
                    prefs.edit().putString(KEY_URL, url).apply()
                    webView.loadUrl(url)
                }
            }
            .show()
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menu.add(0, MENU_URL, 0, getString(R.string.server_url))
        menu.add(0, MENU_RELOAD, 1, getString(R.string.reload))
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean = when (item.itemId) {
        MENU_URL -> { promptForUrl(); true }
        MENU_RELOAD -> { webView.reload(); true }
        else -> super.onOptionsItemSelected(item)
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (webView.canGoBack()) webView.goBack() else @Suppress("DEPRECATION") super.onBackPressed()
    }

    override fun onPause() {
        super.onPause()
        CookieManager.getInstance().flush() // 登录态落盘
    }

    override fun onDestroy() {
        webView.destroy()
        super.onDestroy()
    }

    companion object {
        private const val KEY_URL = "server_url"
        private const val REQ_MIC = 1
        private const val MENU_URL = 10
        private const val MENU_RELOAD = 11
    }
}
