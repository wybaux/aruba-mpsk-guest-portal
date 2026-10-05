import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    APP_TITLE: str = "Wi-Fi Invités Homelab"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    SECRET_KEY: str = "homelab_guest_wifi_secret_key"
    ADMIN_PASSWORD: str = "admin123"

    # Default Wi-Fi Network parameters
    WIFI_SSID: str = "Public-Test"
    WIFI_PASSWORD: Optional[str] = None
    WIFI_SECURITY: str = "WPA"  # WPA, WEP, nopass

    # Aruba Integration
    ARUBA_MODE: str = "mock"  # mock, instant, central
    
    # Aruba Instant Configuration
    ARUBA_INSTANT_HOST: Optional[str] = "https://192.168.1.1:4343"
    ARUBA_INSTANT_USERNAME: Optional[str] = "admin"
    ARUBA_INSTANT_PASSWORD: Optional[str] = "admin"
    ARUBA_INSTANT_VERIFY_SSL: bool = False
    ARUBA_MPSK_PROFILE: str = "MPSK_GUEST"

    # Aruba Central Configuration
    ARUBA_CENTRAL_BASE_URL: Optional[str] = "https://eu-apigw.central.arubanetworks.com"
    ARUBA_CENTRAL_CLIENT_ID: Optional[str] = None
    ARUBA_CENTRAL_CLIENT_SECRET: Optional[str] = None
    ARUBA_CENTRAL_CUSTOMER_ID: Optional[str] = None
    ARUBA_CENTRAL_TOKENS_FILE: Optional[str] = "aruba_tokens.json"

    # Cleanup background check (seconds)
    CLEANUP_CHECK_INTERVAL: int = 60

    # SMTP & Notification Settings
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_FROM: str = "wifi-guest@entreprise.local"
    SMTP_TLS: bool = True
    
    # Sponsorship & OTP Verification Policies
    REQUIRE_OTP_VERIFICATION: bool = False
    REQUIRE_GUEST_EMAIL: bool = False  # Make guest email mandatory in "Coordonnées & Réception par Email"
    ALLOWED_SPONSOR_DOMAINS: Optional[str] = None  # Comma-separated (e.g. "entreprise.com,societe.fr")

    # Localization
    DEFAULT_LANGUAGE: str = "fr"  # Default fallback language: fr, en, es, de, pt

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
