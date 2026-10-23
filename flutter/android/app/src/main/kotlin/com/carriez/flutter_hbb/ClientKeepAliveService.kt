package com.carriez.flutter_hbb

import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.PendingIntent.FLAG_IMMUTABLE
import android.app.PendingIntent.FLAG_UPDATE_CURRENT
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.provider.Settings
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel

const val CLIENT_KEEP_ALIVE_NOTIFY_ID = 1000
const val CLIENT_KEEP_ALIVE_CHANNEL_ID = "RustDeskClientKeepAlive"

/**
 * Keeps the process and network alive while this device is controlling a
 * remote peer and the app is backgrounded.
 *
 * Client (outgoing/controller) role only — unrelated to [MainService],
 * which is the opposite role (this device being controlled) and is not
 * touched by this class.
 */
class ClientKeepAliveService : Service() {
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForegroundNotification()
        acquireWakeLock()
        return START_NOT_STICKY
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        super.onTaskRemoved(rootIntent)
        stopSelf()
    }

    override fun onDestroy() {
        stopForeground(STOP_FOREGROUND_REMOVE)
        releaseWakeLock()
        super.onDestroy()
    }

    private fun acquireWakeLock() {
        if (wakeLock != null) return
        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = powerManager.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "rustdesk:client_keep_alive"
        ).apply {
            setReferenceCounted(false)
            acquire()
        }
    }

    private fun releaseWakeLock() {
        wakeLock?.let {
            if (it.isHeld) {
                it.release()
            }
        }
        wakeLock = null
    }

    private fun startForegroundNotification() {
        val notificationManager =
            getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val channelId = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CLIENT_KEEP_ALIVE_CHANNEL_ID,
                "RustDesk Connection",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Keeps an active outgoing RustDesk connection alive in the background"
                lightColor = Color.BLUE
                lockscreenVisibility = Notification.VISIBILITY_PRIVATE
            }
            notificationManager.createNotificationChannel(channel)
            CLIENT_KEEP_ALIVE_CHANNEL_ID
        } else {
            ""
        }

        val intent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED
            action = Intent.ACTION_MAIN
            addCategory(Intent.CATEGORY_LAUNCHER)
        }
        val pendingIntent = PendingIntent.getActivity(
            this, 0, intent, FLAG_UPDATE_CURRENT or FLAG_IMMUTABLE
        )

        val notification = NotificationCompat.Builder(this, channelId)
            .setOngoing(true)
            .setSmallIcon(R.mipmap.ic_stat_logo)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setContentTitle(DEFAULT_NOTIFY_TITLE)
            .setContentText(translate("Connection is active in the background"))
            .setOnlyAlertOnce(true)
            .setContentIntent(pendingIntent)
            .setColor(ContextCompat.getColor(this, R.color.primary))
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                CLIENT_KEEP_ALIVE_NOTIFY_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
            )
        } else {
            startForeground(CLIENT_KEEP_ALIVE_NOTIFY_ID, notification)
        }
    }

    companion object {
        // Same tag MainActivity used for these calls before they moved here.
        private const val logTag = "mMainActivity"

        /**
         * Handles the client keep-alive methods of the flutter method channel.
         * Returns true when [call] was one of them, false otherwise.
         */
        fun onMethodCall(
            activity: Activity,
            call: MethodCall,
            result: MethodChannel.Result
        ): Boolean {
            when (call.method) {
                "start_client_keep_alive" -> {
                    Intent(activity, ClientKeepAliveService::class.java).also {
                        ContextCompat.startForegroundService(activity, it)
                    }
                    result.success(true)
                }
                "stop_client_keep_alive" -> {
                    activity.stopService(Intent(activity, ClientKeepAliveService::class.java))
                    result.success(true)
                }
                "request_ignore_battery_optimizations" -> {
                    val powerManager =
                        activity.getSystemService(Context.POWER_SERVICE) as PowerManager
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M &&
                        !powerManager.isIgnoringBatteryOptimizations(activity.packageName)
                    ) {
                        try {
                            val intent = Intent(
                                Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                                Uri.parse("package:${activity.packageName}")
                            )
                            activity.startActivity(intent)
                        } catch (e: Exception) {
                            Log.e(logTag, "Failed to request battery optimization exemption", e)
                        }
                    }
                    result.success(true)
                }
                else -> return false
            }
            return true
        }
    }
}
