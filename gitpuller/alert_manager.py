from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple
from .state_manager import StateManager, InMemoryStateManager, MageAIStateManager

class AlertManager:    
    def __init__(self, state_manager: Optional[StateManager] = None, use_mage_ai: bool = False):
        if state_manager is not None:
            self.state_manager = state_manager
        elif use_mage_ai:
            try:
                self.state_manager = MageAIStateManager()
            except ImportError:
                print("⚠️ Mage AI not available, falling back to in-memory state manager")
                self.state_manager = InMemoryStateManager()
        else:
            self.state_manager = InMemoryStateManager()
    
    def should_send_alert(
        self, 
        pipeline_uuid: str, 
        current_error: str, 
        suppression_hours: int = 1
    ) -> Tuple[bool, Dict[str, Any]]:
        
        state = self.state_manager.load_alert_state(pipeline_uuid)
        last_error = state.get("last_error_message")
        last_alert_time_str = state.get("last_alert_time")
        
        # If no previous alert, always send
        if not last_error or not last_alert_time_str:
            return True, {"last_error_message": None, "last_alert_time": None}
        
        # If error is different, always send
        if last_error != current_error:
            return True, {"last_error_message": last_error, "last_alert_time": last_alert_time_str}
        
        # Same error - check if enough time has passed
        try:
            last_alert_time = datetime.fromisoformat(last_alert_time_str)
            time_since_last_alert = datetime.now() - last_alert_time
            
            if time_since_last_alert >= timedelta(hours=suppression_hours):
                return True, {"last_error_message": last_error, "last_alert_time": last_alert_time_str}
            else:
                return False, {"last_error_message": last_error, "last_alert_time": last_alert_time_str}
        except (ValueError, TypeError):
            # If we can't parse the time, send the alert to be safe
            return True, {"last_error_message": last_error, "last_alert_time": last_alert_time_str}
    
    def save_alert_state(
        self, 
        pipeline_uuid: str, 
        error_message: str, 
        alert_time: datetime, 
        pipeline_status: str
    ) -> None:
        """Save alert state."""
        self.state_manager.save_alert_state(pipeline_uuid, error_message, alert_time, pipeline_status)
    
    def clear_alert_state(self, pipeline_uuid: str) -> None:
        """Clear alert state on success."""
        self.state_manager.clear_alert_state(pipeline_uuid)

