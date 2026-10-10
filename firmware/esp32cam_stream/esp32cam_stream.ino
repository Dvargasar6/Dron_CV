/*
 * esp32cam_stream.ino - Camera server for the drone vision system.
 *
 * Board: AI-Thinker ESP32-CAM (Arduino-ESP32 core 2.0.x or 3.x, PSRAM enabled)
 *
 * Exposes two HTTP servers (two servers so that control requests still work
 * while a client is consuming the video stream):
 *
 *   Port 80
 *     GET /                         -> small HTML page with the live stream (browser test)
 *     GET /status                   -> JSON with camera + WiFi status
 *     GET /capture                  -> one JPEG frame
 *     GET /control?var=<v>&val=<x>  -> change a setting, replies with /status JSON
 *          framesize  QQVGA|QVGA|CIF|VGA|SVGA|XGA|HD|SXGA|UXGA
 *          quality    4..63   (JPEG, lower = better)
 *          brightness -2..2   contrast -2..2
 *          vflip 0|1          hmirror 0|1        led 0|1 (flash LED)
 *
 *   Port 81
 *     GET /stream                   -> MJPEG stream (multipart/x-mixed-replace)
 *
 * The ESP32 prints its IP address on the serial monitor (115200 baud) at boot.
 */

#include "esp_camera.h"
#include "esp_http_server.h"
#include <WiFi.h>

#include "camera_pins.h"
#include "wifi_config.h"

// ----------------------------------------------------------------------------
// MJPEG multipart constants
// ----------------------------------------------------------------------------
#define PART_BOUNDARY "droneframeboundary"
static const char *STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;
static const char *STREAM_BOUNDARY     = "\r\n--" PART_BOUNDARY "\r\n";
static const char *STREAM_PART         = "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

static httpd_handle_t control_httpd = NULL;
static httpd_handle_t stream_httpd  = NULL;

static String g_ip = "0.0.0.0";
static bool   g_led_on = false;

// ----------------------------------------------------------------------------
// Frame size <-> name table (names are used by the PC so it does not depend
// on the numeric values of framesize_t, which changed between library versions)
// ----------------------------------------------------------------------------
struct FrameSizeName {
  const char  *name;
  framesize_t  size;
};

static const FrameSizeName FRAME_SIZES[] = {
  {"QQVGA", FRAMESIZE_QQVGA}, {"QVGA", FRAMESIZE_QVGA}, {"CIF",  FRAMESIZE_CIF},
  {"VGA",   FRAMESIZE_VGA},   {"SVGA", FRAMESIZE_SVGA}, {"XGA",  FRAMESIZE_XGA},
  {"HD",    FRAMESIZE_HD},    {"SXGA", FRAMESIZE_SXGA}, {"UXGA", FRAMESIZE_UXGA},
};
static const size_t N_FRAME_SIZES = sizeof(FRAME_SIZES) / sizeof(FRAME_SIZES[0]);

static bool framesize_from_name(const char *name, framesize_t *out) {
  for (size_t i = 0; i < N_FRAME_SIZES; i++) {
    if (strcasecmp(name, FRAME_SIZES[i].name) == 0) {
      *out = FRAME_SIZES[i].size;
      return true;
    }
  }
  return false;
}

static const char *framesize_name(framesize_t size) {
  for (size_t i = 0; i < N_FRAME_SIZES; i++) {
    if (FRAME_SIZES[i].size == size) return FRAME_SIZES[i].name;
  }
  return "OTHER";
}

// ----------------------------------------------------------------------------
// Camera
// ----------------------------------------------------------------------------
static bool init_camera() {
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0       = Y2_GPIO_NUM;
  config.pin_d1       = Y3_GPIO_NUM;
  config.pin_d2       = Y4_GPIO_NUM;
  config.pin_d3       = Y5_GPIO_NUM;
  config.pin_d4       = Y6_GPIO_NUM;
  config.pin_d5       = Y7_GPIO_NUM;
  config.pin_d6       = Y8_GPIO_NUM;
  config.pin_d7       = Y9_GPIO_NUM;
  config.pin_xclk     = XCLK_GPIO_NUM;
  config.pin_pclk     = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM;
  config.pin_href     = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn     = PWDN_GPIO_NUM;
  config.pin_reset    = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;   // JPEG is compressed on the sensor -> cheap to send

  if (psramFound()) {
    config.frame_size   = FRAMESIZE_VGA;
    config.jpeg_quality = 12;
    config.fb_count     = 2;
    config.fb_location  = CAMERA_FB_IN_PSRAM;
    config.grab_mode    = CAMERA_GRAB_LATEST;   // always send the newest frame (low latency)
  } else {
    Serial.println("WARNING: no PSRAM found, limiting to QVGA");
    config.frame_size   = FRAMESIZE_QVGA;
    config.jpeg_quality = 15;
    config.fb_count     = 1;
    config.fb_location  = CAMERA_FB_IN_DRAM;
    config.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed with error 0x%x\n", err);
    return false;
  }
  return true;
}

// ----------------------------------------------------------------------------
// HTTP handlers
// ----------------------------------------------------------------------------
static esp_err_t status_handler(httpd_req_t *req) {
  sensor_t *s = esp_camera_sensor_get();
  char json[384];
  int n = snprintf(json, sizeof(json),
                   "{\"framesize\":\"%s\",\"quality\":%d,\"brightness\":%d,\"contrast\":%d,"
                   "\"vflip\":%d,\"hmirror\":%d,\"led\":%d,\"rssi\":%d,\"ip\":\"%s\","
                   "\"ap_mode\":%d,\"uptime_ms\":%lu,\"free_psram\":%u}",
                   framesize_name(s->status.framesize), s->status.quality,
                   s->status.brightness, s->status.contrast, s->status.vflip,
                   s->status.hmirror, g_led_on ? 1 : 0, (int)WiFi.RSSI(), g_ip.c_str(),
                   (WiFi.getMode() & WIFI_MODE_AP) ? 1 : 0, (unsigned long)millis(),
                   (unsigned)ESP.getFreePsram());
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, json, n);
}

static esp_err_t control_handler(httpd_req_t *req) {
  char query[128], var[32], val[32];
  if (httpd_req_get_url_query_str(req, query, sizeof(query)) != ESP_OK ||
      httpd_query_key_value(query, "var", var, sizeof(var)) != ESP_OK ||
      httpd_query_key_value(query, "val", val, sizeof(val)) != ESP_OK) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "use /control?var=<name>&val=<value>");
    return ESP_FAIL;
  }

  sensor_t *s = esp_camera_sensor_get();
  int res = 0;
  if (!strcmp(var, "framesize")) {
    framesize_t fs;
    if (!framesize_from_name(val, &fs)) res = -1;
    else res = s->set_framesize(s, fs);
  } else if (!strcmp(var, "quality")) {
    res = s->set_quality(s, atoi(val));
  } else if (!strcmp(var, "brightness")) {
    res = s->set_brightness(s, atoi(val));
  } else if (!strcmp(var, "contrast")) {
    res = s->set_contrast(s, atoi(val));
  } else if (!strcmp(var, "vflip")) {
    res = s->set_vflip(s, atoi(val));
  } else if (!strcmp(var, "hmirror")) {
    res = s->set_hmirror(s, atoi(val));
  } else if (!strcmp(var, "led")) {
    g_led_on = atoi(val) != 0;
    digitalWrite(LED_GPIO_NUM, g_led_on ? HIGH : LOW);
  } else {
    res = -1;
  }

  if (res != 0) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "invalid var or val");
    return ESP_FAIL;
  }
  Serial.printf("control: %s = %s\n", var, val);
  return status_handler(req);
}

static esp_err_t capture_handler(httpd_req_t *req) {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }
  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=capture.jpg");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  esp_err_t res = httpd_resp_send(req, (const char *)fb->buf, fb->len);
  esp_camera_fb_return(fb);
  return res;
}

static esp_err_t stream_handler(httpd_req_t *req) {
  char part_buf[64];
  esp_err_t res = httpd_resp_set_type(req, STREAM_CONTENT_TYPE);
  if (res != ESP_OK) return res;
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  Serial.println("stream: client connected");
  while (true) {
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
      Serial.println("stream: camera capture failed");
      res = ESP_FAIL;
      break;
    }
    res = httpd_resp_send_chunk(req, STREAM_BOUNDARY, strlen(STREAM_BOUNDARY));
    if (res == ESP_OK) {
      size_t hlen = snprintf(part_buf, sizeof(part_buf), STREAM_PART, (unsigned)fb->len);
      res = httpd_resp_send_chunk(req, part_buf, hlen);
    }
    if (res == ESP_OK) {
      res = httpd_resp_send_chunk(req, (const char *)fb->buf, fb->len);
    }
    esp_camera_fb_return(fb);
    if (res != ESP_OK) break;   // client closed the connection
  }
  Serial.println("stream: client disconnected");
  return res;
}

static const char INDEX_HTML[] =
  "<!doctype html><html><head><meta name='viewport' content='width=device-width'>"
  "<title>Drone Cam</title></head><body style='background:#111;color:#eee;font-family:sans-serif'>"
  "<h3>Drone ESP32-CAM</h3><img id='s' style='max-width:100%'>"
  "<p><a style='color:#8cf' href='/status'>/status</a> | <a style='color:#8cf' href='/capture'>/capture</a></p>"
  "<script>document.getElementById('s').src='http://'+location.hostname+':81/stream';</script>"
  "</body></html>";

static esp_err_t index_handler(httpd_req_t *req) {
  httpd_resp_set_type(req, "text/html");
  return httpd_resp_send(req, INDEX_HTML, strlen(INDEX_HTML));
}

static void start_servers() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = 80;

  httpd_uri_t index_uri   = {.uri = "/",        .method = HTTP_GET, .handler = index_handler,   .user_ctx = NULL};
  httpd_uri_t status_uri  = {.uri = "/status",  .method = HTTP_GET, .handler = status_handler,  .user_ctx = NULL};
  httpd_uri_t control_uri = {.uri = "/control", .method = HTTP_GET, .handler = control_handler, .user_ctx = NULL};
  httpd_uri_t capture_uri = {.uri = "/capture", .method = HTTP_GET, .handler = capture_handler, .user_ctx = NULL};
  httpd_uri_t stream_uri  = {.uri = "/stream",  .method = HTTP_GET, .handler = stream_handler,  .user_ctx = NULL};

  if (httpd_start(&control_httpd, &config) == ESP_OK) {
    httpd_register_uri_handler(control_httpd, &index_uri);
    httpd_register_uri_handler(control_httpd, &status_uri);
    httpd_register_uri_handler(control_httpd, &control_uri);
    httpd_register_uri_handler(control_httpd, &capture_uri);
  } else {
    Serial.println("ERROR: could not start control server (port 80)");
  }

  config.server_port += 1;   // 81
  config.ctrl_port   += 1;
  if (httpd_start(&stream_httpd, &config) == ESP_OK) {
    httpd_register_uri_handler(stream_httpd, &stream_uri);
  } else {
    Serial.println("ERROR: could not start stream server (port 81)");
  }
}

// ----------------------------------------------------------------------------
// WiFi
// ----------------------------------------------------------------------------
static void start_access_point() {
  WiFi.mode(WIFI_AP);
  WiFi.softAP(AP_SSID, AP_PASSWORD);
  g_ip = WiFi.softAPIP().toString();
  Serial.printf("Access Point '%s' started\n", AP_SSID);
}

static void start_wifi() {
#if USE_AP_MODE
  start_access_point();
#else
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);   // WiFi power save adds a lot of latency to the stream
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("Connecting to '%s'", WIFI_SSID);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < WIFI_CONNECT_TIMEOUT_MS) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    g_ip = WiFi.localIP().toString();
  } else {
    Serial.println("Could not join the router -> falling back to Access Point mode");
    start_access_point();
  }
#endif
}

// ----------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  Serial.println();
  Serial.println("=== Drone ESP32-CAM stream server ===");

  pinMode(LED_GPIO_NUM, OUTPUT);
  digitalWrite(LED_GPIO_NUM, LOW);

  if (!init_camera()) {
    Serial.println("Halting: fix the camera and reset the board.");
    while (true) delay(1000);
  }

  start_wifi();
  start_servers();

  Serial.println("Ready:");
  Serial.printf("  Browser test : http://%s/\n", g_ip.c_str());
  Serial.printf("  Stream       : http://%s:81/stream\n", g_ip.c_str());
  Serial.printf("  Status       : http://%s/status\n", g_ip.c_str());
}

void loop() {
  // In station mode, try to rejoin the router if the link drops.
  static uint32_t last_check = 0;
  if (millis() - last_check > 5000) {
    last_check = millis();
    if (WiFi.getMode() == WIFI_STA && WiFi.status() != WL_CONNECTED) {
      Serial.println("WiFi lost, reconnecting...");
      WiFi.reconnect();
    }
  }
  delay(100);
}
