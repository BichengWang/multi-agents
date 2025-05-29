"""
Modal Configuration Settings
"""
import os
from typing import Optional

class ModalConfig:
    """Configuration class for Modal app settings"""
    
    # Default Modal app URL - replace with your actual deployment URL
    DEFAULT_MODAL_APP_URL = "https://modal.com/apps/ey10923/main/ap-MpAQH9mDyzSiBsYhOuL19T"
    
    # API endpoints
    GENERATE_ENDPOINT = "/generate"
    HEALTH_ENDPOINT = "/"
    
    # Request settings
    DEFAULT_TIMEOUT = 30
    DEFAULT_MAX_TOKENS = 500
    
    @classmethod
    def get_modal_app_url(cls) -> str:
        """
        Get Modal app URL from environment variable or use default
        
        Returns:
            str: Modal app URL
        """
        return os.getenv("MODAL_APP_URL", cls.DEFAULT_MODAL_APP_URL)
    
    @classmethod
    def get_generate_url(cls) -> str:
        """
        Get the full URL for the generate endpoint
        
        Returns:
            str: Full generate endpoint URL
        """
        base_url = cls.get_modal_app_url()
        return f"{base_url}{cls.GENERATE_ENDPOINT}"
    
    @classmethod
    def get_health_url(cls) -> str:
        """
        Get the full URL for the health check endpoint
        
        Returns:
            str: Full health check endpoint URL
        """
        base_url = cls.get_modal_app_url()
        return f"{base_url}{cls.HEALTH_ENDPOINT}"
    
    @classmethod
    def get_timeout(cls) -> int:
        """
        Get request timeout from environment or use default
        
        Returns:
            int: Timeout in seconds
        """
        try:
            return int(os.getenv("MODAL_TIMEOUT", cls.DEFAULT_TIMEOUT))
        except ValueError:
            return cls.DEFAULT_TIMEOUT
    
    @classmethod
    def get_max_tokens(cls) -> int:
        """
        Get max tokens from environment or use default
        
        Returns:
            int: Maximum tokens
        """
        try:
            return int(os.getenv("MODAL_MAX_TOKENS", cls.DEFAULT_MAX_TOKENS))
        except ValueError:
            return cls.DEFAULT_MAX_TOKENS

# Convenience functions for direct access
def get_modal_app_url() -> str:
    """Get Modal app URL"""
    return ModalConfig.get_modal_app_url()

def get_generate_url() -> str:
    """Get generate endpoint URL"""
    return ModalConfig.get_generate_url()

def get_health_url() -> str:
    """Get health check endpoint URL"""
    return ModalConfig.get_health_url() 