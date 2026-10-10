// WiFi settings for the ESP32-CAM. EDIT THIS FILE before flashing.
#pragma once

// 0 = Station mode: the ESP32 joins your existing router (SSID/PASSWORD below).
// 1 = Access Point mode: the ESP32 creates its own network (AP_SSID/AP_PASSWORD).
//     The PC must connect to that network; the ESP32 IP is then 192.168.4.1.
#define USE_AP_MODE        0

// --- Station mode (join a router) ---
#define WIFI_SSID          "YOUR_WIFI_SSID"
#define WIFI_PASSWORD      "YOUR_WIFI_PASSWORD"
// If joining the router fails after this many ms, fall back to AP mode.
#define WIFI_CONNECT_TIMEOUT_MS  20000

// --- Access Point mode (own network) ---
#define AP_SSID            "DRONE_CAM"
#define AP_PASSWORD        "drone1234"   // min. 8 characters
