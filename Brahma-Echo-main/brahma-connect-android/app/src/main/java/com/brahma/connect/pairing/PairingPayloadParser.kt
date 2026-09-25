package com.brahma.connect.pairing

import com.brahma.connect.core.PairingOffer
import org.json.JSONObject

object PairingPayloadParser {
    fun parse(raw: String): PairingOffer? {
        if (raw.isBlank()) return null
        return try {
            val json = JSONObject(raw)
            // New desktop builds use short keys to make monitor QR scanning
            // reliable; retain the original JSON form for older builds.
            if (json.has("h") && json.has("t")) {
                PairingOffer(
                    service = "_BRAHMA._tcp.local.",
                    host = json.optString("h"),
                    port = json.optInt("p", 8765),
                    pairingToken = json.optString("t"),
                    pairingCode = json.optString("c"),
                    expiresInSeconds = 300,
                    createdAt = "",
                )
            } else {
                PairingOffer.fromJson(json)
            }
        } catch (_: Exception) {
            null
        }
    }
}
