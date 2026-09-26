package com.brahma.connect

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import com.brahma.connect.core.AgentStateStore
import com.brahma.connect.pairing.PairingStorage
import com.brahma.connect.ui.BrahmaConnectApp
import com.brahma.connect.ui.theme.BrahmaConnectTheme

class MainActivity : ComponentActivity() {
    private lateinit var storage: PairingStorage
    private var pendingServiceStart = false
    private var cameraGranted by mutableStateOf(false)
    private var notificationsGranted by mutableStateOf(false)

    private val cameraPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        // Compose observes this state, so Scan QR becomes usable immediately
        // after the Android permission sheet is accepted.
        cameraGranted = granted
    }

    private val notificationPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        notificationsGranted = granted || Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU
        if (granted || Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            if (pendingServiceStart) {
                pendingServiceStart = false
                startGatewayService()
            }
        } else {
            pendingServiceStart = false
            AgentStateStore.setError("Notification permission is required for Brahma Connect.")
            AgentStateStore.setStatus("Notification permission denied")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        cameraGranted = ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED
        notificationsGranted = Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
        storage = PairingStorage(this)
        AgentStateStore.setCredential(storage.loadCredential())
        storage.loadGatewayHint()?.let {
            AgentStateStore.setPairingOffer(it)
            AgentStateStore.setGateway(
                com.brahma.connect.core.GatewayEndpoint(
                    name = "Brahma PC",
                    host = it.host,
                    port = it.port,
                )
            )
        }
        maybeStartService()
        setContent {
            BrahmaConnectTheme {
                BrahmaConnectApp(
                    cameraGranted = cameraGranted,
                    notificationsGranted = notificationsGranted,
                    onRequestCameraPermission = {
                        cameraPermission.launch(Manifest.permission.CAMERA)
                    },
                    onRequestNotificationPermission = {
                        notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
                    },
                    onStartService = { maybeStartService() },
                )
            }
        }
    }

    private fun maybeStartService() {
        val endpoint = AgentStateStore.gateway.value
        if (endpoint != null || AgentStateStore.credential.value != null) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
            ) {
                pendingServiceStart = true
                notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
                return
            }
            startGatewayService()
        }
    }

    private fun startGatewayService() {
        val endpoint = AgentStateStore.gateway.value
        if (endpoint != null || AgentStateStore.credential.value != null) {
            val intent = Intent(this, BrahmaConnectForegroundService::class.java)
            ContextCompat.startForegroundService(this, intent)
        }
    }
}
