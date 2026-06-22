"""Pluggable storage backends for alert-suppression state.

``InMemoryStateManager`` keeps state for the lifetime of the process; the Mage
variant persists it across pipeline runs via Mage global variables.
"""

from typing import Any, Dict, Optional
from datetime import datetime


class StateManager:
    """Abstract interface for persisting per-pipeline alert state."""

    def load_alert_state(self, pipeline_uuid: str) -> Dict[str, Any]:
        """Load alert state for a pipeline."""
        raise NotImplementedError
    
    def save_alert_state(
        self, 
        pipeline_uuid: str, 
        error_message: str, 
        alert_time: datetime, 
        pipeline_status: str
    ) -> None:
        """Save alert state for a pipeline."""
        raise NotImplementedError
    
    def clear_alert_state(self, pipeline_uuid: str) -> None:
        """Clear alert state for a pipeline."""
        raise NotImplementedError

class InMemoryStateManager(StateManager):
    """Process-local state. Note: state is lost when the process exits, so
    alert suppression only works within a single run unless a persistent
    backend (e.g. Mage) is used."""

    def __init__(self):
        self._state: Dict[str, Dict[str, Any]] = {}
    
    def load_alert_state(self, pipeline_uuid: str) -> Dict[str, Any]:
        """Load alert state from memory."""
        return self._state.get(pipeline_uuid, {
            "last_alert_time": None,
            "last_error_message": None
        })
    
    def save_alert_state(
        self, 
        pipeline_uuid: str, 
        error_message: str, 
        alert_time: datetime, 
        pipeline_status: str
    ) -> None:
        """Save alert state to memory."""
        self._state[pipeline_uuid] = {
            "last_alert_time": alert_time.isoformat(),
            "last_error_message": error_message,
            "pipeline_status": pipeline_status,
            "last_alert_timestamp": alert_time.isoformat()
        }
    
    def clear_alert_state(self, pipeline_uuid: str) -> None:
        """Clear alert state from memory."""
        if pipeline_uuid in self._state:
            self._state[pipeline_uuid] = {
                "last_alert_time": None,
                "last_error_message": None,
                "pipeline_status": "success"
            }

class MageAIStateManager(StateManager):
    """Persists alert state across runs using Mage AI global variables."""

    def __init__(self):
        # Import lazily so the package works outside a Mage environment;
        # AlertManager catches this ImportError to fall back to in-memory.
        try:
            from mage_ai.data_preparation.variable_manager import (
                set_global_variable,
                get_global_variable
            )
            self._set_global_variable = set_global_variable
            self._get_global_variable = get_global_variable
        except ImportError:
            raise ImportError(
                "Mage AI is not installed. Install it with: pip install mage-ai"
            )
    
    def load_alert_state(self, pipeline_uuid: str) -> Dict[str, Any]:
        try:
            last_error_message = self._get_global_variable(pipeline_uuid, 'last_error_message')
            last_alert_time = self._get_global_variable(pipeline_uuid, 'last_alert_time')
            
            if last_error_message and last_alert_time:
                return {
                    "last_alert_time": last_alert_time,
                    "last_error_message": last_error_message
                }
        except Exception as e:
            print(f"⚠️ Warning: Could not load alert state from global variables: {e}")
        
        return {
            "last_alert_time": None,
            "last_error_message": None
        }
    
    def save_alert_state(
        self, 
        pipeline_uuid: str, 
        error_message: str, 
        alert_time: datetime, 
        pipeline_status: str
    ) -> None:
        try:
            self._set_global_variable(pipeline_uuid, 'last_error_message', error_message)
            self._set_global_variable(pipeline_uuid, 'last_alert_time', alert_time.isoformat())
            self._set_global_variable(pipeline_uuid, 'pipeline_status', pipeline_status)
            self._set_global_variable(pipeline_uuid, 'last_alert_timestamp', alert_time.isoformat())
            print(f"✅ Saved alert state to global variables: error_message, pipeline_status={pipeline_status}")
        except Exception as e:
            print(f"⚠️ Warning: Could not save alert state to global variables: {e}")
    
    def clear_alert_state(self, pipeline_uuid: str) -> None:
        try:
            self._set_global_variable(pipeline_uuid, 'last_error_message', None)
            self._set_global_variable(pipeline_uuid, 'last_alert_time', None)
            self._set_global_variable(pipeline_uuid, 'pipeline_status', 'success')
            print("✅ Cleared alert state from global variables")
        except Exception as e:
            print(f"⚠️ Warning: Could not clear alert state: {e}")