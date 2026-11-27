# Distributed Experimentation & Statistical Infrastructure — V1

## Objective

Build an extensible, distributed statistical computing platform that allows data scientists, statisticians, and product teams to bring their own databases, SQL metric definitions, and statistical engines.

The platform is responsible for orchestrating data retrieval, distributed computation, and result persistence.

## Core Requirements

### 1. Database Integration
- Support user-provided database connections.
- Provide an extensible database connector interface.
- Support PostgreSQL as the initial database provider.
- Securely manage database credentials and read-only access.

### 2. Metric Definitions
- Allow users to register reusable, parameterized SQL metric templates.
- Support experiment identifiers, date ranges, and other analysis parameters.
- Allow users to write SQL according to their own database schema and dialect.
- Support version-controlled metric definitions.

### 3. Pluggable Statistical Engine
- Allow users to register custom Python statistical engines.
- Define a standardized input and output contract for statistical computation.
- Provide a reference statistical engine supporting:
  - Continuous metrics.
  - Proportional metrics.
  - Treatment effects.
  - Standard errors.
  - Confidence intervals.
  - P-values.

### 4. Distributed Job Processing
- Support asynchronous analysis job submission through Kafka.
- Support concurrent execution across multiple workers.
- Retrieve metric data and execute the selected statistical engine.
- Handle job failures, retries, and duplicate processing.
- Track job execution status.

### 5. Infrastructure
- Containerize individual services using Docker.
- Run workers on a local, self-managed Kubernetes cluster.
- Support configurable worker replicas and resource allocation.
- Support worker failure recovery.
- Provide basic logging and execution monitoring.

### 6. API and Result Persistence
- Expose a REST API for submitting analysis jobs, checking status, and retrieving results.
- Persist analysis results and execution metadata in PostgreSQL.
- Track metric definition versions, statistical engine versions, and analysis parameters for traceability.

### 7. Reference Implementation
- Provide a synthetic experiment dataset.
- Provide sample SQL metric templates.
- Provide a functional reference Python statistical engine.
- Demonstrate end-to-end distributed analysis.
- Demonstrate that alternative metric templates, database connections, and statistical engines can be integrated without modifying the core infrastructure.

## V1 Acceptance Criteria

- Multiple analysis jobs can execute concurrently.
- Workers can be independently scaled.
- Failed jobs can be retried without duplicating persisted results.
- Users can integrate custom statistical engines and metric definitions.
- Database-specific logic remains independent of core job orchestration.
- Analysis results are retrievable and traceable to their original configurations.

## Out of Scope — V2

- React dashboard.
- Scheduled analysis.
- Additional database providers.
- Kafka-based worker autoscaling.
- Advanced statistical methods.
- Dedicated analytical warehouse.
- Production-grade observability.
- Multi-tenancy and untrusted statistical engine execution.
- Experiment assignment and feature flag management.