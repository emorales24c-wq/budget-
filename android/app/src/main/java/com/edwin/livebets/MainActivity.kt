package com.edwin.livebets

import android.Manifest
import android.app.Activity
import android.content.pm.PackageManager
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.webkit.WebViewAssetLoader
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.latin.TextRecognizerOptions
import org.json.JSONObject

class MainActivity : Activity() {

    private lateinit var web: WebView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.statusBarColor = Color.parseColor("#0A0A0F")
        window.navigationBarColor = Color.parseColor("#0A0A0F")

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }

        // Without this, Doze cuts the app's network while the screen is off
        val pm = getSystemService(PowerManager::class.java)
        if (!pm.isIgnoringBatteryOptimizations(packageName)) {
            try {
                startActivity(
                    Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName"))
                )
            } catch (e: Exception) { }
        }

        // Serve the bundled UI from an https origin so fetch + localStorage behave like on the web
        val loader = WebViewAssetLoader.Builder()
            .addPathHandler("/assets/", WebViewAssetLoader.AssetsPathHandler(this))
            .build()

        web = WebView(this)
        web.setBackgroundColor(Color.parseColor("#0A0A0F"))
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.webViewClient = object : WebViewClient() {
            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? =
                loader.shouldInterceptRequest(request.url)
        }
        web.addJavascriptInterface(Bridge(), "Android")
        setContentView(web)
        web.loadUrl("https://appassets.androidplatform.net/assets/index.html")
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else super.onBackPressed()
    }

    // Foto de Kalshi: el usuario escoge un screenshot, ML Kit lee el texto y se lo pasa a la página
    private val PICK_PHOTO = 7

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != PICK_PHOTO) return
        val uri = data?.data
        if (resultCode != RESULT_OK || uri == null) { sendOcr(null, "cancel"); return }
        try {
            val image = InputImage.fromFilePath(this, uri)
            TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS).process(image)
                .addOnSuccessListener { sendOcr(it.text, null) }
                .addOnFailureListener { sendOcr(null, it.message ?: "error") }
        } catch (e: Exception) { sendOcr(null, e.message ?: "error") }
    }

    private fun sendOcr(text: String?, err: String?) {
        val js = "window.onOcr && window.onOcr(" + JSONObject.quote(text ?: "") + "," + JSONObject.quote(err ?: "") + ")"
        runOnUiThread { web.evaluateJavascript(js, null) }
    }

    inner class Bridge {
        @JavascriptInterface
        fun pickPhoto() {
            runOnUiThread {
                val i = Intent(Intent.ACTION_GET_CONTENT).apply { type = "image/*"; addCategory(Intent.CATEGORY_OPENABLE) }
                @Suppress("DEPRECATION")
                startActivityForResult(Intent.createChooser(i, "Screenshot de Kalshi"), PICK_PHOTO)
            }
        }

        // Called by the page whenever the bet list changes
        @JavascriptInterface
        fun syncBets(json: String) {
            BetStore.save(this@MainActivity, json)
            BetService.start(this@MainActivity)
        }
    }
}
