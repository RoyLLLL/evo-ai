"""
A2A Protocol Implementation using Official a2a-sdk 0.3.0

Migrated from custom JSON-RPC implementation to use the official a2a-python SDK.
Preserves all existing business logic while leveraging SDK abstractions.

Key Features:
- Uses AgentExecutor pattern from a2a-sdk
- Integrates with existing agent_runner
- Preserves session management and conversation history
- Supports file handling and push notifications
- Full streaming support via EventQueue
- API key authentication
"""

import uuid
import logging
import base64
import httpx
from datetime import datetime
from typing import Dict, Any, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.sql import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import (
    TaskUpdater,
    DatabaseTaskStore,
    TaskStore,
    BasePushNotificationSender,
    DatabasePushNotificationConfigStore,
)
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.apps import A2AStarletteApplication
from a2a.types import (
    AgentCard,
    AgentCapabilities,
    AgentSkill,
    AgentProvider,
    Task,
    TaskState,
    Part,
    TextPart,
    FilePart,
    FileWithBytes,
)
from a2a.utils import new_agent_text_message, new_task
from a2a.utils.errors import ServerError

from src.config.database import get_db
from src.config.settings import settings
from src.services.agent_service import get_agent
from src.services.adk.agent_runner import run_agent, run_agent_stream
from src.services.service_providers import (
    session_service,
    artifacts_service,
    memory_service,
)
from src.schemas.chat import FileData

logger = logging.getLogger(__name__)

# Create async engine for A2A database stores
# Convert postgresql:// to postgresql+asyncpg://
async_connection_string = settings.POSTGRES_CONNECTION_STRING.replace(
    "postgresql://", "postgresql+asyncpg://"
)
async_engine = create_async_engine(
    async_connection_string,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

router = APIRouter(
    prefix="/a2a",
    tags=["a2a-sdk"],
    responses={
        404: {"description": "Not found"},
        400: {"description": "Bad request"},
        401: {"description": "Unauthorized"},
        500: {"description": "Internal server error"},
    },
)


# ============================================================================
# Authentication
# ============================================================================


async def verify_api_key(db: Session, x_api_key: str) -> bool:
    """Verifies API key against agent config."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="API key not provided")

    query = text("SELECT * FROM agents WHERE config->>'api_key' = :api_key LIMIT 1")
    result = db.execute(query, {"api_key": x_api_key}).first()

    if not result:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return True


# ============================================================================
# Helper Functions for Message Processing
# ============================================================================


def extract_text_from_parts(parts: List[Part]) -> str:
    """Extract text from SDK Part objects."""
    for part in parts:
        if hasattr(part, 'root') and isinstance(part.root, TextPart):
            return part.root.text
    return ""


def extract_files_from_parts(parts: List[Part]) -> List[FileData]:
    """Extract files from SDK Part objects and convert to FileData."""
    files = []
    for part in parts:
        if hasattr(part, 'root') and isinstance(part.root, FilePart):
            file_part = part.root
            if hasattr(file_part, 'file') and file_part.file:
                file_obj = file_part.file
                # Handle both FileWithBytes and FileWithUri
                if hasattr(file_obj, 'bytes') and file_obj.bytes:
                    try:
                        # Validate base64
                        base64.b64decode(file_obj.bytes)

                        file_data = FileData(
                            filename=getattr(file_obj, 'name', 'file'),
                            content_type=getattr(file_obj, 'mime_type', 'application/octet-stream'),
                            data=file_obj.bytes,
                        )
                        files.append(file_data)
                        logger.info(f"📎 Extracted file: {file_data.filename}")
                    except Exception as e:
                        logger.error(f"❌ Invalid base64 in file: {e}")
    return files


def clean_message_content(content: str, role: str) -> str:
    """Clean message content to remove JSON artifacts."""
    if not content:
        return ""

    # Remove common JSON artifacts from agent responses
    if role == "agent":
        import re
        # Remove JSON-like structures
        content = re.sub(r'\{[^}]*"text"[^}]*:[^}]*\}', '', content)
        content = re.sub(r'\{[^}]*"content"[^}]*:[^}]*\}', '', content)

    return content.strip()


async def get_conversation_history(
    agent_id: UUID,
    external_id: str,
) -> List[Dict[str, Any]]:
    """
    Retrieve conversation history from session service.

    Returns list of history entries with role, content, messageId, timestamp.
    """
    try:
        session_id = f"{external_id}_{agent_id}"
        session = await session_service.get_session(session_id)

        if not session:
            logger.info(f"No existing session found for {session_id}")
            return []

        events = await session_service.get_session_events(session_id)
        history = []

        for event in events:
            event_type = event.get("type", "")

            if event_type == "user_message":
                content = event.get("content", "")
                history.append({
                    "role": "user",
                    "content": content,
                    "messageId": event.get("id"),
                    "timestamp": event.get("timestamp"),
                })
            elif event_type == "agent_response":
                content = event.get("content", "")
                # Clean agent responses
                content = clean_message_content(content, "agent")
                if content:
                    history.append({
                        "role": "agent",
                        "content": content,
                        "messageId": event.get("id"),
                        "timestamp": event.get("timestamp"),
                    })

        logger.info(f"📚 Retrieved {len(history)} history entries")
        return history

    except Exception as e:
        logger.error(f"Error retrieving conversation history: {e}")
        return []


def extract_history_from_params(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract history from request params according to A2A spec."""
    history = []
    if "history" in params and isinstance(params["history"], list):
        for msg in params["history"]:
            if isinstance(msg, dict) and "role" in msg and "parts" in msg:
                # Extract text from parts
                text_content = ""
                for part in msg["parts"]:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "text"
                        and "text" in part
                    ):
                        text_content += part["text"] + " "

                if text_content.strip():
                    history.append(
                        {
                            "role": msg["role"],
                            "content": text_content.strip(),
                            "messageId": msg.get("messageId"),
                            "timestamp": msg.get("timestamp"),
                        }
                    )

    logger.info(f"📚 Extracted {len(history)} messages from request history")
    return history


def combine_histories(
    request_history: List[Dict[str, Any]], session_history: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Combine request history with session history, avoiding duplicates."""
    combined = []

    # Add session history first
    for msg in session_history:
        combined.append(msg)

    # Add request history, avoiding duplicates based on messageId or content
    for req_msg in request_history:
        # Check if this message already exists in combined history
        is_duplicate = False
        for existing_msg in combined:
            if (
                req_msg.get("messageId")
                and req_msg["messageId"] == existing_msg.get("messageId")
            ) or (
                req_msg["content"] == existing_msg["content"]
                and req_msg["role"] == existing_msg["role"]
            ):
                is_duplicate = True
                break

        if not is_duplicate:
            combined.append(req_msg)

    return combined


# ============================================================================
# EvoAI Agent Executor - Integrates with agent_runner
# ============================================================================


class EvoAIAgentExecutor(AgentExecutor):
    """
    AgentExecutor implementation that integrates with EvoAI's agent_runner.

    Handles:
    - Message extraction from SDK format
    - File processing
    - Session management and history
    - Integration with agent_runner
    - Response streaming via EventQueue
    """

    def __init__(self, db: Session, agent_id: UUID):
        self.db = db
        self.agent_id = agent_id

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """
        Execute agent with message from context.

        This is called by the SDK's DefaultRequestHandler for message/send
        and message/stream methods.
        """
        try:
            logger.info(f"🚀 Executing agent {self.agent_id}")

            # Extract message from context
            if not hasattr(context, 'message') or not context.message:
                logger.error("❌ No message in context")
                await self._emit_error(event_queue, "No message provided")
                return

            message = context.message

            # Extract text and files
            text = self._extract_text(message)
            files = self._extract_files(message)

            if not text:
                logger.error("❌ No text content in message")
                await self._emit_error(event_queue, "No text content found")
                return

            logger.info(f"📝 Message: {text[:100]}...")

            # Get context_id (session identifier)
            context_id = context.context_id or str(uuid.uuid4())
            logger.info(f"📝 Context ID: {context_id}")

            # Get or create task
            task = context.current_task
            if not task:
                task = new_task(message)
                await event_queue.enqueue_event(task)

            # Create task updater for streaming updates
            updater = TaskUpdater(event_queue, task.id, task.context_id)

            # Check if streaming is requested
            is_streaming = getattr(context, 'is_streaming', False)

            if is_streaming:
                # Use streaming execution
                await self._execute_streaming(
                    text, files, context_id, updater
                )
            else:
                # Use non-streaming execution
                await self._execute_non_streaming(
                    text, files, context_id, updater
                )

        except Exception as e:
            logger.error(f"❌ Error in execute(): {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            await self._emit_error(event_queue, f"Execution error: {str(e)}")

    async def _execute_non_streaming(
        self,
        text: str,
        files: List[FileData],
        context_id: str,
        updater: TaskUpdater,
    ) -> None:
        """Execute agent without streaming."""
        try:
            # Call agent_runner
            result = await run_agent(
                agent_id=str(self.agent_id),
                external_id=context_id,
                message=text,
                session_service=session_service,
                artifacts_service=artifacts_service,
                memory_service=memory_service,
                db=self.db,
                files=files if files else None,
            )

            final_response = result.get("final_response", "No response")
            logger.info(f"✅ Agent response: {final_response[:100]}...")

            # Add artifact with response
            await updater.add_artifact(
                [Part(root=TextPart(text=final_response))],
                name='agent_response',
            )

            # Mark task as complete
            await updater.complete()

        except Exception as e:
            logger.error(f"Error in non-streaming execution: {e}")
            raise

    async def _execute_streaming(
        self,
        text: str,
        files: List[FileData],
        context_id: str,
        updater: TaskUpdater,
    ) -> None:
        """Execute agent with streaming."""
        try:
            # Call agent_runner with streaming
            async for chunk in run_agent_stream(
                agent_id=str(self.agent_id),
                external_id=context_id,
                message=text,
                session_service=session_service,
                artifacts_service=artifacts_service,
                memory_service=memory_service,
                db=self.db,
                files=files if files else None,
            ):
                # Send working status with chunk
                await updater.update_status(
                    TaskState.working,
                    new_agent_text_message(
                        chunk,
                        context_id,
                        updater.task_id,
                    ),
                )

            # Mark as complete after streaming finishes
            await updater.complete()

        except Exception as e:
            logger.error(f"Error in streaming execution: {e}")
            raise

    def _extract_text(self, message) -> str:
        """Extract text from SDK message."""
        try:
            if hasattr(message, 'parts') and message.parts:
                return extract_text_from_parts(message.parts)

            if hasattr(message, 'text'):
                return message.text

            if isinstance(message, str):
                return message

            return ""
        except Exception as e:
            logger.error(f"Error extracting text: {e}")
            return ""

    def _extract_files(self, message) -> List[FileData]:
        """Extract files from SDK message."""
        try:
            if hasattr(message, 'parts') and message.parts:
                return extract_files_from_parts(message.parts)
            return []
        except Exception as e:
            logger.error(f"Error extracting files: {e}")
            return []

    async def _emit_error(self, event_queue: EventQueue, error_message: str):
        """Emit error message."""
        try:
            error_msg = new_agent_text_message(f"Error: {error_message}")
            await event_queue.enqueue_event(error_msg)
        except Exception as e:
            logger.error(f"Error emitting error event: {e}")

    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Cancel task execution."""
        logger.info(f"Cancel requested for agent {self.agent_id}")
        # TODO: Implement actual cancellation if needed
        await self._emit_error(event_queue, "Cancellation not yet implemented")


# ============================================================================
# Agent Card Creation
# ============================================================================


def create_agent_card(agent, agent_id: UUID) -> AgentCard:
    """Create AgentCard from agent database model."""

    # Extract skills from agent config
    skills = []
    if agent.config and isinstance(agent.config, dict):
        agent_skills = agent.config.get('skills', [])
        for skill_data in agent_skills:
            if isinstance(skill_data, dict):
                skills.append(AgentSkill(
                    id=skill_data.get('id', str(uuid.uuid4())),
                    name=skill_data.get('name', ''),
                    description=skill_data.get('description', ''),
                    tags=skill_data.get('tags', []),
                    examples=skill_data.get('examples', []),
                ))

    # Create agent card
    agent_card = AgentCard(
        name=agent.name,
        description=agent.description or "AI Agent",
        url=f"{settings.API_URL}/api/v1/a2a/{agent_id}",
        version="1.0.0",
        default_input_modes=["text", "file"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=True,  # Enable push notifications
        ),
        provider=AgentProvider(
            organization=getattr(settings, 'ORGANIZATION_NAME', 'EvoAI'),
            url=getattr(settings, 'ORGANIZATION_URL', settings.API_URL),
        ),
        skills=skills,
    )

    return agent_card


# ============================================================================
# SDK Server Management
# ============================================================================


class A2AServerManager:
    """Manages A2A SDK servers for agents."""

    def __init__(self):
        self.servers: Dict[str, A2AStarletteApplication] = {}
        self.apps: Dict[str, Any] = {}  # Cache built ASGI apps
        self.task_stores: Dict[str, TaskStore] = {}
        self.push_config_stores: Dict[str, DatabasePushNotificationConfigStore] = {}
        self.push_senders: Dict[str, BasePushNotificationSender] = {}
        self.httpx_client = httpx.AsyncClient(timeout=30.0)
        self.async_engine = async_engine  # Use shared async engine

    def get_or_create_server(
        self,
        agent_id: UUID,
        db: Session,
    ) -> Optional[A2AStarletteApplication]:
        """Get existing server or create new one for agent."""
        agent_id_str = str(agent_id)

        if agent_id_str in self.servers:
            return self.servers[agent_id_str]

        try:
            # Get agent from database
            agent = get_agent(db, agent_id)
            if not agent:
                logger.error(f"Agent {agent_id} not found")
                return None

            logger.info(f"🏗️ Creating A2A server for agent: {agent.name}")

            # Create agent card
            agent_card = create_agent_card(agent, agent_id)

            # Create agent executor
            agent_executor = EvoAIAgentExecutor(db, agent_id)

            # Create database task store (shared table for all agents)
            task_store = DatabaseTaskStore(
                engine=self.async_engine,
                create_table=True,  # Auto-create if not exists
                table_name="a2a_tasks",  # Shared table
            )
            self.task_stores[agent_id_str] = task_store

            # Create database push notification config store (shared table)
            push_config_store = DatabasePushNotificationConfigStore(
                engine=self.async_engine,
                create_table=True,  # Auto-create if not exists
                table_name="a2a_push_configs",  # Shared table
                encryption_key=getattr(settings, 'A2A_ENCRYPTION_KEY', None),
            )
            self.push_config_stores[agent_id_str] = push_config_store

            push_sender = BasePushNotificationSender(
                httpx_client=self.httpx_client,
                config_store=push_config_store,
            )
            self.push_senders[agent_id_str] = push_sender

            # Create request handler with database-backed stores
            request_handler = DefaultRequestHandler(
                agent_executor=agent_executor,
                task_store=task_store,
                push_config_store=push_config_store,
                push_sender=push_sender,
            )

            # Create Starlette application
            server = A2AStarletteApplication(
                agent_card=agent_card,
                http_handler=request_handler,
            )

            self.servers[agent_id_str] = server

            # Build and cache the ASGI app (performance optimization)
            self.apps[agent_id_str] = server.build()

            logger.info(f"✅ A2A server created with database stores for agent {agent_id}")

            return server

        except Exception as e:
            logger.error(f"Error creating A2A server: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def get_app(self, agent_id: UUID) -> Optional[Any]:
        """Get cached ASGI app for agent."""
        agent_id_str = str(agent_id)
        return self.apps.get(agent_id_str)

    def remove_server(self, agent_id: UUID) -> bool:
        """Remove server from cache."""
        agent_id_str = str(agent_id)
        removed = False

        if agent_id_str in self.servers:
            del self.servers[agent_id_str]
            removed = True

        if agent_id_str in self.apps:
            del self.apps[agent_id_str]

        if agent_id_str in self.task_stores:
            del self.task_stores[agent_id_str]

        if agent_id_str in self.push_config_stores:
            del self.push_config_stores[agent_id_str]

        if agent_id_str in self.push_senders:
            del self.push_senders[agent_id_str]

        return removed

    async def cleanup(self):
        """Cleanup resources."""
        if self.httpx_client:
            await self.httpx_client.aclose()


# Global server manager
server_manager = A2AServerManager()


# ============================================================================
# FastAPI Routes
# ============================================================================


@router.api_route(
    "/{agent_id}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def handle_a2a_request(
    agent_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    x_api_key: str = Header(None, alias="x-api-key"),
):
    """
    Main A2A endpoint - delegates to SDK server.

    Handles all A2A JSON-RPC methods:
    - message/send
    - message/stream
    - tasks/get
    - tasks/cancel
    - agent/authenticatedExtendedCard
    """
    # Verify API key
    await verify_api_key(db, x_api_key)

    # Get or create SDK server for this agent
    server = server_manager.get_or_create_server(agent_id, db)
    if not server:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get cached ASGI app (performance optimization - no rebuild on each request)
    app = server_manager.get_app(agent_id)
    if not app:
        raise HTTPException(status_code=500, detail="Failed to get agent app")

    # Forward request to SDK server
    from starlette.requests import Request as StarletteRequest

    # Convert FastAPI request to Starlette request
    starlette_request = StarletteRequest(request.scope, request.receive)

    # Call the app
    response = await app(starlette_request.scope, starlette_request.receive, request._send)

    return response


@router.get("/{agent_id}/.well-known/agent.json")
async def get_agent_card_endpoint(
    agent_id: UUID,
    db: Session = Depends(get_db),
):
    """
    Agent card discovery endpoint (A2A spec compliant).
    """
    try:
        agent = get_agent(db, agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        agent_card = create_agent_card(agent, agent_id)

        # Convert to dict for JSON response
        return agent_card.model_dump(mode='json', exclude_none=True)

    except Exception as e:
        logger.error(f"Error getting agent card: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "a2a-sdk",
        "version": "0.3.0",
        "protocol": "A2A 0.3.0",
        "implementation": "official-sdk",
    }


# ============================================================================
# Custom Extension Endpoints (Beyond A2A Spec)
# ============================================================================


@router.get("/{agent_id}/sessions")
async def list_agent_sessions(
    agent_id: UUID,
    external_id: Optional[str] = None,
    db: Session = Depends(get_db),
    x_api_key: str = Header(None, alias="x-api-key"),
):
    """
    List sessions for an agent (custom extension).

    Query params:
    - external_id: Filter by external_id
    """
    await verify_api_key(db, x_api_key)

    try:
        # Get sessions from session service
        if external_id:
            session_id = f"{external_id}_{agent_id}"
            session = await session_service.get_session(session_id)
            sessions = [session] if session else []
        else:
            # List all sessions for this agent
            # Note: This requires session_service to support listing
            sessions = []
            logger.warning("Listing all sessions not fully implemented")

        return {
            "agent_id": str(agent_id),
            "sessions": [
                {
                    "session_id": s.get("id"),
                    "external_id": s.get("external_id"),
                    "created_at": s.get("created_at"),
                    "updated_at": s.get("updated_at"),
                }
                for s in sessions if s
            ],
        }

    except Exception as e:
        logger.error(f"Error listing sessions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{agent_id}/sessions/{session_id}/history")
async def get_session_history(
    agent_id: UUID,
    session_id: str,
    db: Session = Depends(get_db),
    x_api_key: str = Header(None, alias="x-api-key"),
):
    """
    Get conversation history for a session (custom extension).
    """
    await verify_api_key(db, x_api_key)

    try:
        # Get history from session service
        events = await session_service.get_session_events(session_id)

        history = []
        for event in events:
            event_type = event.get("type", "")

            if event_type == "user_message":
                history.append({
                    "role": "user",
                    "content": event.get("content", ""),
                    "timestamp": event.get("timestamp"),
                })
            elif event_type == "agent_response":
                content = clean_message_content(event.get("content", ""), "agent")
                if content:
                    history.append({
                        "role": "agent",
                        "content": content,
                        "timestamp": event.get("timestamp"),
                    })

        return {
            "agent_id": str(agent_id),
            "session_id": session_id,
            "history": history,
        }

    except Exception as e:
        logger.error(f"Error getting session history: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{agent_id}/conversation/history")
async def get_conversation_history_endpoint(
    agent_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    x_api_key: str = Header(None, alias="x-api-key"),
):
    """
    Get conversation history via JSON-RPC (custom extension).

    Method: conversation/history
    Params: { contextId: string }
    """
    await verify_api_key(db, x_api_key)

    try:
        body = await request.json()

        # Validate JSON-RPC format
        if body.get("jsonrpc") != "2.0":
            raise HTTPException(status_code=400, detail="Invalid JSON-RPC version")

        method = body.get("method")
        if method != "conversation/history":
            raise HTTPException(status_code=400, detail=f"Unsupported method: {method}")

        params = body.get("params", {})
        context_id = params.get("contextId")

        if not context_id:
            return {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "error": {
                    "code": -32602,
                    "message": "Invalid params",
                    "data": {"missing": "contextId"},
                },
            }

        # Get history
        history = await get_conversation_history(agent_id, context_id)

        return {
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "result": {
                "history": history,
                "contextId": context_id,
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in conversation/history: {e}")
        return {
            "jsonrpc": "2.0",
            "id": body.get("id") if "body" in locals() else None,
            "error": {
                "code": -32603,
                "message": "Internal error",
                "data": {"error": str(e)},
            },
        }
