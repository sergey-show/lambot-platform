import os


KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC_EVENTS = os.getenv("KAFKA_TOPIC_EVENTS", "platform.events")
KAFKA_GROUP_ORCHESTRATOR = os.getenv("KAFKA_GROUP_ORCHESTRATOR", "orchestrator-service")
KAFKA_TOPIC_AGENT_TASKS = os.getenv("KAFKA_TOPIC_AGENT_TASKS", "platform.agent.tasks")
KAFKA_TOPIC_AGENT_RESULTS = os.getenv("KAFKA_TOPIC_AGENT_RESULTS", "platform.agent.results")
KAFKA_GROUP_AGENT_WORKER = os.getenv("KAFKA_GROUP_AGENT_WORKER", "agent-worker-service")
KAFKA_GROUP_AGENT_BOOTSTRAP = os.getenv("KAFKA_GROUP_AGENT_BOOTSTRAP", "agent-bootstrap-service")
