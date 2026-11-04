flowchart TB
  subgraph orchestrator["Orchestrator (orchestration_runner)"]
    intake[intake: full / direct + sa?]
    sa[SA: spec, glossary, traceability, tracker]
    narrative[PM, architect, *_plan, implementation, deploy, QA]
    execplan[execution_plan: JSON batch]
  end

  subgraph llm["LLM"]
    chat[Chat + JSON envelope]
    react[LangGraph ReAct + tools]
  end

  subgraph storage["Postgres (admin-service)"]
    run[AITeamRun]
    steps[AITeamStep]
    tasks[AITeamExecutionTask]
  end

  subgraph workers["Workers (Kafka)"]
    ssh[ssh_run_command]
    k8s[kubernetes_query]
    ansible[ansible_run_template]
  end

  intake --> sa
  sa --> narrative
  narrative --> chat
  narrative --> react
  react -.->|get_project_docs_context on demand| storage
  narrative --> execplan
  execplan --> tasks
  tasks --> workers
  workers --> tasks
  steps --> run
  tasks --> run
  orchestrator --> storage
  narrative -->|DoD gate deploy/pm_summary| orchestrator
