#define AP_NAME  "WeatherLamp"
#define USER_AGENT  "WeatherLamp/0.1.0"
// #define NUM_LEDS 16
#ifndef FastLED
  #include <FastLED.h>
#endif
#define LED_PIN     D4
#define CLK_PIN     D5
#define BRIGHTNESS  150
#define LED_TYPE    WS2812B
// #define LED_TYPE    WS2812B
// #define LED_TYPE    LPD8806
#define COLOR_ORDER GRB
CRGB leds[NUM_LEDS];
#define UPDATES_PER_SECOND 50
