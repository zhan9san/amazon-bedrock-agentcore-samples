#!/usr/bin/env python3

import argparse
import boto3
import json
import logging
import os
import time
from pathlib import Path
from dotenv import load_dotenv
from botocore.exceptions import ClientError

# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,
    # Define log message format
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)


def _write_agent_arn_to_file(agent_arn: str, output_dir: str = None) -> None:
    """Write agent ARN to .agent_arn file."""
    if output_dir is None:
        output_dir = Path(__file__).parent
    else:
        output_dir = Path(output_dir)

    arn_file = output_dir / ".agent_arn"

    try:
        with open(arn_file, "w") as f:
            f.write(agent_arn)
        logging.info(f"💾 Agent Runtime ARN saved to {arn_file}")
    except Exception as e:
        logging.error(f"Failed to write agent ARN to file: {e}")


def _get_agent_runtime_id_by_name(client: boto3.client, runtime_name: str) -> str:
    """Get agent runtime ID by name."""
    try:
        response = client.list_agent_runtimes()
        agent_runtimes = response.get("agentRuntimes", [])

        for runtime in agent_runtimes:
            if runtime["agentRuntimeName"] == runtime_name:
                return runtime["agentRuntimeId"]

        return None

    except ClientError as e:
        logging.error(f"Failed to get agent runtime ID: {e}")
        return None


def _delete_agent_runtime(client: boto3.client, runtime_id: str) -> bool:
    """Delete an agent runtime by ID."""
    try:
        logging.info(f"Deleting agent runtime with ID: {runtime_id}")
        client.delete_agent_runtime(agentRuntimeId=runtime_id)
        logging.info("Agent runtime deleted successfully")
        return True

    except ClientError as e:
        logging.error(f"Failed to delete agent runtime: {e}")
        return False


def _list_existing_agent_runtimes(client: boto3.client) -> None:
    """List all existing agent runtimes."""
    try:
        response = client.list_agent_runtimes()
        agent_runtimes = response.get("agentRuntimes", [])

        if not agent_runtimes:
            logging.info("No existing agent runtimes found.")
            return

        logging.info("Existing agent runtimes:")
        for runtime in agent_runtimes:
            logging.info(json.dumps(runtime, indent=2, default=str))

    except ClientError as e:
        logging.error(f"Failed to list agent runtimes: {e}")


def _create_agent_runtime(
    client: boto3.client,
    runtime_name: str,
    container_uri: str,
    role_arn: str,
    api_key: str,
    gateway_access_token: str,
    llm_provider: str = "bedrock",
    force_recreate: bool = False,
) -> None:
    """Create an agent runtime with error handling for conflicts."""
    # Build environment variables
    env_vars = {
        "GATEWAY_ACCESS_TOKEN": gateway_access_token,
        "LLM_PROVIDER": llm_provider,
    }

    # Only add ANTHROPIC_API_KEY if it exists
    if llm_provider == "anthropic":
        env_vars["ANTHROPIC_API_KEY"] = api_key
    elif llm_provider == "openai":
        env_vars["OPENAI_API_KEY"] = api_key

    # Check for DEBUG environment variable
    debug_mode = os.getenv("DEBUG", "false")
    if debug_mode.lower() in ("true", "1", "yes"):
        env_vars["DEBUG"] = "true"
        logging.info("Debug mode enabled for agent runtime")

    # Log environment variables being passed to AgentCore (mask sensitive data)
    logging.info("🚀 Environment variables being passed to AgentCore Runtime:")
    for key, value in env_vars.items():
        if key in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GATEWAY_ACCESS_TOKEN"]:
            masked_value = f"{'*' * 20}...{value[-8:] if len(value) > 8 else '***'}"
            logging.info(f"   {key}: {masked_value}")
        else:
            logging.info(f"   {key}: {value}")
    try:
        response = client.create_agent_runtime(
            agentRuntimeName=runtime_name,
            agentRuntimeArtifact={
                "containerConfiguration": {"containerUri": container_uri}
            },
            networkConfiguration={"networkMode": "PUBLIC"},
            roleArn=role_arn,
            environmentVariables=env_vars,
        )

        logging.info(f"Agent Runtime created successfully!")
        logging.info(f"Agent Runtime ARN: {response['agentRuntimeArn']}")
        logging.info(f"Status: {response['status']}")
        _write_agent_arn_to_file(response["agentRuntimeArn"])

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")

        # Handle non-conflict errors immediately
        if error_code != "ConflictException":
            logging.error(f"Failed to create agent runtime: {e}")
            raise

        # Handle conflict - runtime already exists
        logging.error(f"Agent runtime '{runtime_name}' already exists.")
        logging.info("Listing existing agent runtimes:")
        _list_existing_agent_runtimes(client)

        # If not forcing recreate, provide guidance and exit
        if not force_recreate:
            logging.info(
                "Please retry with a new agent name using the --runtime-name parameter, or use --force-recreate to delete and recreate."
            )
            return

        # Handle force recreate scenario
        logging.info(
            "Force recreate requested, attempting to delete existing runtime..."
        )
        runtime_id = _get_agent_runtime_id_by_name(client, runtime_name)

        if not runtime_id:
            logging.error(f"Could not find runtime ID for '{runtime_name}'")
            return

        if not _delete_agent_runtime(client, runtime_id):
            logging.error("Failed to delete existing runtime")
            return

        # Wait for deletion to complete
        logging.info("Waiting 10 seconds for deletion to complete...")
        time.sleep(10)

        # Recreate the runtime after successful deletion
        logging.info("Attempting to recreate agent runtime...")
        response = client.create_agent_runtime(
            agentRuntimeName=runtime_name,
            agentRuntimeArtifact={
                "containerConfiguration": {"containerUri": container_uri}
            },
            networkConfiguration={"networkMode": "PUBLIC"},
            roleArn=role_arn,
            environmentVariables=env_vars,
        )

        logging.info(f"Agent Runtime recreated successfully!")
        logging.info(f"Agent Runtime ARN: {response['agentRuntimeArn']}")
        logging.info(f"Status: {response['status']}")
        _write_agent_arn_to_file(response["agentRuntimeArn"])


def main():
    parser = argparse.ArgumentParser(
        description="Deploy SRE Agent to AgentCore Runtime"
    )
    parser.add_argument(
        "--runtime-name",
        default="sre-agent",
        help="Name for the agent runtime (default: sre-agent)",
    )
    parser.add_argument(
        "--container-uri",
        required=True,
        help="Container URI (e.g., account-id.dkr.ecr.us-west-2.amazonaws.com/my-agent:latest)",
    )
    parser.add_argument(
        "--role-arn", required=True, help="IAM role ARN for the agent runtime"
    )
    parser.add_argument(
        "--region", default="us-east-1", help="AWS region (default: us-east-1)"
    )
    parser.add_argument(
        "--force-recreate",
        action="store_true",
        help="Delete existing runtime if it exists and recreate it",
    )

    args = parser.parse_args()

    # Load environment variables from .env file
    script_dir = Path(__file__).parent
    env_file = script_dir / ".env"

    if env_file.exists():
        load_dotenv(env_file)
        logging.info(f"Loaded environment variables from {env_file}")
    else:
        logging.error(f".env file not found at {env_file}")
        raise FileNotFoundError(
            f"Please create a .env file at {env_file} with GATEWAY_ACCESS_TOKEN and optionally ANTHROPIC_API_KEY"
        )

    # Get environment variables
    llm_provider = os.getenv("LLM_PROVIDER", "bedrock")
    if llm_provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
    elif llm_provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
    else:
        api_key = None
    gateway_access_token = os.getenv("GATEWAY_ACCESS_TOKEN")

    # Log environment variable values (mask sensitive data)
    logging.info("📋 Environment variables loaded:")
    logging.info(f"   LLM_PROVIDER: {llm_provider}")
    if api_key:
        logging.info(
            f"   API_KEY: {'*' * 20}...{api_key[-8:] if len(api_key) > 8 else '***'}"
        )
    else:
        logging.info(
            "   ANTHROPIC_API_KEY or OPENAI_API_KEY: Not set - Amazon Bedrock will be used as the provider"
        )

    if gateway_access_token:
        logging.info(
            f"   GATEWAY_ACCESS_TOKEN: {'*' * 20}...{gateway_access_token[-8:] if len(gateway_access_token) > 8 else '***'}"
        )

    if not gateway_access_token:
        logging.error("GATEWAY_ACCESS_TOKEN not found in .env")
        raise ValueError("GATEWAY_ACCESS_TOKEN must be set in .env")

    client = boto3.client("bedrock-agentcore-control", region_name=args.region)

    _create_agent_runtime(
        client=client,
        runtime_name=args.runtime_name,
        container_uri=args.container_uri,
        role_arn=args.role_arn,
        api_key=api_key,
        gateway_access_token=gateway_access_token,
        llm_provider=llm_provider,
        force_recreate=args.force_recreate,
    )


if __name__ == "__main__":
    main()
