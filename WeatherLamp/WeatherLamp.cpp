/**************************************************************************************
  Sketch to illuminate RGB LED strip using rain forecast
  Copyright 2020-2021 Aapo Rista
  MIT license
 **************************************************************************************/

/*
TODO: 
- 
*/


#include <FS.h> // this needs to be first, or it all crashes and burns...
#include "settings.h" // Remember to copy settings-example.h to settings.h and check all the values!
#include <Wire.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <WiFiManager.h>      // https://github.com/tzapu/WiFiManager WiFi Configuration Magic
#include <ESP8266HTTPClient.h>
#ifndef FastLED
#include <FastLED.h>
#endif

// I2C settings
// #define SDA     D2
// #define SCL     D1

// Define URL, lat and lon default values here, if there are different values in config.json, they are overwritten.
char http_url[150] = "http://weatherlamp.rista.net/v1";
// Helsinki city centre
char latitude[16] = "60.172";
char longitude[16] = "24.945";
char color_map[33] = "plain";
char interval[4] = "30";  // minutes
char slots[5];
uint8_t slots_i = NUM_LEDS;
uint8_t led_array[NUM_LEDS*3];
uint8_t erase_pin = 12; // 13 == D7

// Move to settings, perhaps?
CRGBPalette16 currentPalette;
TBlendType currentBlending;
uint8_t currentMode = '0';
uint8_t brightness = BRIGHTNESS;
static uint8_t startIndex = 0;
uint8_t colorIndex = 0;

uint8_t r = 0;
uint8_t g = 0;
uint8_t b = 0;

// Define and set up all variables / objects
WiFiClient wifiClient;
WiFiManager wifiManager;
String mac_str;
byte mac[6];
char macAddr[13];
char builddate[25];
char serverPath[250];
unsigned long lastPing = 0;

// Flag for saving data
bool shouldSaveConfig = false;

// Callback notifying us of the need to save config
void saveConfigCallback () {
  shouldSaveConfig = true;
  Serial.print("Should save config: ");
  Serial.println(shouldSaveConfig);
}

void requestData();
void runLedEffect();
void setupSpiffs();
void saveConfig();

void set_vars()
{
    // Hard coded slots for now (before implementing scaling from slots to NUM_LEDS)
    itoa(NUM_LEDS, slots, 10);

    strcpy(builddate, __DATE__);
    strcat(builddate, " ");
    strcat(builddate, __TIME__);

    strcpy(serverPath, http_url);
    strcat(serverPath, "?lat=");
    strcat(serverPath, latitude);
    strcat(serverPath, "&lon=");
    strcat(serverPath, longitude);
    strcat(serverPath, "&colormap=");
    strcat(serverPath, color_map);
    strcat(serverPath, "&interval=");
    strcat(serverPath, interval);
    strcat(serverPath, "&slots=");
    strcat(serverPath, slots);
    strcat(serverPath, "&client=");
    strcat(serverPath, macAddr);
    slots_i = atoi(slots);
}

void setup()
{
  Serial.begin(115200);
  Serial.println();
  Serial.println();
  pinMode(erase_pin, INPUT); 
  delay(500);
  uint8_t erase_config = digitalRead(erase_pin);
  Serial.print("Should erase config: ");
  Serial.println(erase_config);

  setupSpiffs();
  wifiManager.setSaveConfigCallback(saveConfigCallback);

  WiFiManagerParameter custom_text1("<p>Data URL</p>");
  wifiManager.addParameter(&custom_text1);
  WiFiManagerParameter custom_http_url("http_url", "Data URL", http_url, 150);
  wifiManager.addParameter(&custom_http_url);

  WiFiManagerParameter custom_text2("<p>Latitude and longitude (max 3 decimals)</p>");
  wifiManager.addParameter(&custom_text2);
  WiFiManagerParameter custom_latitude("latitude", "Latitude ° (60.172)", latitude, 7);
  wifiManager.addParameter(&custom_latitude);
  WiFiManagerParameter custom_longitude("longitude", "Longitude ° (24.945)", longitude, 8);
  wifiManager.addParameter(&custom_longitude);

  WiFiManagerParameter custom_text3("<p>Time interval in minutes</p>");
  wifiManager.addParameter(&custom_text3);
  WiFiManagerParameter custom_interval("interval", "Interval in minutes (30)", interval, 3);
  wifiManager.addParameter(&custom_interval);

  WiFiManagerParameter custom_text4("<p>Number of time slots</p>");
  wifiManager.addParameter(&custom_text4);
  itoa(NUM_LEDS, slots, 10);
  char slots_text[30];
  sprintf(slots_text, "Slots (%s)", slots);
  WiFiManagerParameter custom_slots("slots", slots_text, slots, 2);
  wifiManager.addParameter(&custom_slots);

  WiFiManagerParameter custom_text5("<p>Color map name</p>");
  wifiManager.addParameter(&custom_text5);
  WiFiManagerParameter custom_color_map("color_map", "Color map", color_map, 32);
  wifiManager.addParameter(&custom_color_map);

  // Add all your parameters here
  // Reset settings - wipe credentials for testing
  // wifiManager.resetSettings();
  mac_str = WiFi.macAddress();
  WiFi.macAddress(mac);

  sprintf(macAddr, "%2X%2X%2X%2X%2X%2X", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  char ap_name[30];
  sprintf(ap_name, "%s_%s", AP_NAME, macAddr);
  Serial.print("AP name would be: ");
  Serial.println(ap_name);
  wifiManager.autoConnect(ap_name);

  // Wire.begin(SDA, SCL);
  // read updated parameters
  strcpy(http_url, custom_http_url.getValue());
  strcpy(latitude, custom_latitude.getValue());
  strcpy(longitude, custom_longitude.getValue());
  strcpy(interval, custom_interval.getValue());
  strcpy(slots, custom_slots.getValue());
  strcpy(color_map, custom_color_map.getValue());
  Serial.println(http_url);
  Serial.println(latitude);
  Serial.println(longitude);
  Serial.println(interval);
  Serial.println(slots);
  Serial.println(color_map);

  // Save the custom parameters to FS
  Serial.print("Setup Should save config: ");
  Serial.println(shouldSaveConfig);
  if (shouldSaveConfig) {
    saveConfig();
    shouldSaveConfig = false;
  }
  set_vars();

  Serial.println("Init FastLED");
#ifdef CLKPIN
  FastLED.addLeds<LED_TYPE, DATAPIN, CLKPIN, COLOR_ORDER>(leds, NUM_LEDS).setCorrection( TypicalLEDStrip );
#else
  FastLED.addLeds<LED_TYPE, DATAPIN, COLOR_ORDER>(leds, NUM_LEDS).setCorrection(TypicalLEDStrip);
#endif

  FastLED.setBrightness(BRIGHTNESS);

  currentPalette = RainbowColors_p;
  currentBlending = LINEARBLEND;
}

void setupSpiffs() 
{
  //clean FS, for testing
  //Serial.println("Format FS...");
  //SPIFFS.format();

  // Read configuration from FS json
  Serial.println("Mounting FS...");

  if (SPIFFS.begin()) {
    Serial.println("mounted file system");
    if (SPIFFS.exists("/config.json")) {
      //file exists, reading and loading
      Serial.println("reading config file");
      File configFile = SPIFFS.open("/config.json", "r");
      if (configFile) {
        Serial.println("opened config file");
        size_t size = configFile.size();
        // Allocate a buffer to store contents of the file.
        std::unique_ptr<char[]> buf(new char[size]);
        configFile.readBytes(buf.get(), size);
        DynamicJsonDocument jsonBuffer(1024);
        auto error = deserializeJson(jsonBuffer, buf.get());
        serializeJson(jsonBuffer, Serial);
        if (!error) {
          Serial.println("\nParsed json");
          strcpy(http_url, jsonBuffer["http_url"]);
          strcpy(latitude, jsonBuffer["latitude"]);
          strcpy(longitude, jsonBuffer["longitude"]);
          strcpy(slots, jsonBuffer["slots"]);
          strcpy(interval, jsonBuffer["interval"]);
          strcpy(color_map, jsonBuffer["color_map"]);
        } else {
          Serial.println("Failed to load json config");
        }
      }
    }
  } else {
    Serial.println("Failed to mount FS");
  }
}

void saveConfig() 
{
  Serial.println("Saving config");
  DynamicJsonDocument jsonBuffer(1024);
  jsonBuffer["http_url"] = http_url;
  jsonBuffer["latitude"] = latitude;
  jsonBuffer["longitude"] = longitude;
  jsonBuffer["interval"] = interval;
  jsonBuffer["slots"] = slots;
  jsonBuffer["color_map"] = color_map;
  File configFile = SPIFFS.open("/config.json", "w");
  if (!configFile) {
    Serial.println("failed to open config file for writing");
  }
  serializeJsonPretty(jsonBuffer, Serial);
  serializeJson(jsonBuffer, configFile);
  configFile.close();
}

void requestData()
{
  if (WiFi.status() == WL_CONNECTED)
  {
    HTTPClient http;
    http.addHeader("X-Client-Id", macAddr);
    http.addHeader("X-Build-Date", builddate);
    http.setUserAgent(USER_AGENT);
    Serial.println(serverPath);

    // Your Domain name with URL path or IP address with path
    http.begin(serverPath);

    // Send HTTP GET request
    int httpResponseCode = http.GET();

    if (httpResponseCode > 0)
    {
      Serial.print("HTTP Response code: ");
      Serial.println(httpResponseCode);
      Serial.print("NUM_LEDS: ");
      Serial.println(NUM_LEDS);
      //int s = atoi(slots);
      Serial.print("Slots: ");
      Serial.println(slots);
      String payload = http.getString();
      Serial.println(payload);
      currentPalette = CRGBPalette16();
      for (uint8_t i = 0; i < slots_i; i++) {
        uint8_t i1 = i*4;
        uint8_t i2 = i*4 + 1;
        uint8_t i3 = i*4 + 2;
        // uint8_t wind = i*4 + 3;
        uint8_t r = (uint8_t)payload[i1];
        uint8_t g = (uint8_t)payload[i2];
        uint8_t b = (uint8_t)payload[i3];
        led_array[i*3] = r;
        led_array[i*3+1] = g;
        led_array[i*3+2] = b;
        Serial.print(i);
        Serial.print(": ");
        Serial.print(i*3);
        Serial.print(": ");
        Serial.print(r);
        Serial.print(",");
        Serial.print(g);
        Serial.print(",");
        Serial.print(b);
        Serial.println();
        //currentPalette[i] = CRGB(r, g, b);
      }
    }
    else
    {
      Serial.print("Error code: ");
      Serial.println(httpResponseCode);
    }
    // Free resources
    http.end();
  }
  else
  {
    Serial.println("WiFi Disconnected");
  }
}

void loop()
{
  unsigned long now = millis();
  if (lastPing + 10000 < now)
  {
    uint8_t erase_config = digitalRead(erase_pin);
    Serial.print("Should erase config: ");
    Serial.println(erase_config);
    Serial.print("Build date: ");
    Serial.println(builddate);
    requestData();
    lastPing = now;
  }
  runLedEffect();
  FastLED.show();
  FastLED.delay(1000 / UPDATES_PER_SECOND);
}

void FillLEDsFromPaletteColors(uint8_t colorIndex)
{
  for (uint8_t i = 0; i < NUM_LEDS; i++)
  {
    leds[i] = ColorFromPalette(currentPalette, colorIndex, brightness, currentBlending);
    colorIndex += (int)255/NUM_LEDS;
  }
}

void FillLEDsWithSolidColor()
{
  for (uint8_t i = 0; i < NUM_LEDS; i++)
  {
    leds[i].setRGB(r, g, b);
  }
}

void FillLEDsWithWeatherColor()
{
  uint8_t r;
  uint8_t g;
  uint8_t b;
  for (uint8_t i = 0; i < slots_i; i++)
  {
    r = (uint8_t)led_array[i*3];
    g = (uint8_t)led_array[i*3+1];
    b = (uint8_t)led_array[i*3+2];
    leds[i].setRGB(r, g, b);
  }
}


void runLedEffect()
{
  // Serial.println(currentMode);
  currentMode = '2';
  switch (currentMode)
  {
  case '0':
    static uint8_t startIndex = 0;
    // startIndex = startIndex + 1; /* motion speed */
    FillLEDsFromPaletteColors(startIndex);
    break;
  case '1':
    FillLEDsWithSolidColor();
    break;
  case '2':
    FillLEDsWithWeatherColor();
    break;
  default:
    Serial.println("Got invalid mode (in runLedEffect)");
    break;
  }
}
