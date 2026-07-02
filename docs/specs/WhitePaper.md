# Intelligent Proxy Service Architecture

## 1\. Core Design Philosophy

Building upon V2.0, this plan introduces two core optimizations to address precision scheduling and high-frequency write bottlenecks, upgrading to an architecture of **"State Tiering & Precision Scheduling"**.

* **State Tiering**: We will clearly distinguish between **"Low-Frequency Persistent State"** and **"High-Frequency Volatile State"**.  
  * **Physical Proxy Data** (IP, Port, validation results), as low-frequency core data, will be managed by **PostgreSQL** for persistence, ensuring its integrity and historical traceability. **We will no longer delete any proxy records**, instead using a status field (is\_active) to mark their availability.  
  * **Business Feedback Data** (scores, success/failure counts for each source), as high-frequency business state, will be **kept in-memory** for all read and write operations. This dramatically improves the response speed of the /get-proxy and /feedback endpoints. This in-memory state will be persisted through periodic snapshots and graceful shutdowns using a **Pickle file**.  
* **Precision Scheduling**: The triggering mechanism for background tasks will adopt **Fixed-Rate Scheduling**. This ensures that the starting point for task execution is fixed and unaffected by the duration of the task itself, thus preventing "schedule drift."

## 2\. System Architecture & Components (Key Changes)

1. **Scheduler**  
   * **Upgraded Scheduling Mechanism**: The scheduler will no longer use a simple sleep(interval) model. It will run an independent timer thread that places a "validation task" into a task queue at a **fixed interval** (e.g., every 60 seconds). Worker threads will then execute tasks from this queue. This guarantees a constant frequency for task initiation, resolving the ambiguity between "every 5 minutes" and "5 minutes after the last one finished."  
2. **Parallel Validator**  
   * **Optimized Validation Scope**: The validation task will no longer perform a full table scan. It will execute a more precise SQL query to select targets, focusing **only on proxies that have never been validated or were previously active**. This significantly reduces the number of useless validations and improves efficiency.  
     SELECT id, protocol, ip, port FROM proxies  
     WHERE last\_validated\_at IS NULL OR (is\_active \= true AND last\_validated\_at \< NOW() \- INTERVAL '30 minutes');

3. **State Manager**  
   * This new logical layer is responsible for managing the tiered state.  
   * **On Startup**:  
     1. Loads all is\_active \= true proxies from the PostgreSQL proxies table to build a basic **in-memory pool of physically available proxies**.  
     2. Attempts to load the **in-memory business feedback data** from the source\_stats.pkl file from the last session.  
     3. Merges both to form a complete, immediately-servable state.  
   * **During Runtime**:  
     * The /get-proxy and /feedback endpoints interact **only with the in-memory business feedback data**, achieving millisecond-level responses.  
   * **On Shutdown/Snapshot**:  
     * Dumps the complete in-memory business feedback data to the source\_stats.pkl file.

## 3\. Database & Data Structures (Key Changes)

1. **proxies Table**  
   * **Data Policy**: **Never delete**. The is\_active field becomes the sole criterion for a proxy's current availability. This preserves the historical record of all proxies for future analysis.  
2. **source\_proxy\_stats Table Removed**  
   * The functionality of this table is completely migrated to an **in-memory data structure** and its corresponding source\_stats.pkl persistence file.  
   * **In-memory Structure Example**:  
     self.source\_stats \= {  
         "my\_app": {  
             "http://1.2.3.4:8080": {  
                 "score": 10,  
                 "success\_count": 20,  
                 \# ...  
             },  
             \# ...  
         },  
         \# ...  
     }

## 4\. Core Workflows (Optimized)

1. **Startup Workflow (Optimized)**  
   1. The service starts and connects to PostgreSQL.  
   2. It executes SELECT \* FROM proxies WHERE is\_active \= true; to load all physically available proxies into an in-memory ActiveProxySet.  
   3. It attempts to load business feedback data from source\_stats.pkl. If successful, it merges this with the ActiveProxySet to restore the complete, ready-to-use proxy pool.  
   4. The **Scheduler** and **API Server** are started.  
2. **Background Automation Workflow (Optimized)**  
   1. The **Fetcher** scrapes proxies from various sources according to their independent schedules and writes them to the PostgreSQL proxies table.  
   2. The **Scheduler** pushes a "validate" command to the task queue at a **fixed interval** (e.g., every 60 seconds).  
   3. A **Validator** worker thread picks up the command, executes the optimized SQL query, performs parallel validation, and updates the results (is\_active, latency\_ms, etc.) back into the proxies table.  
   4. **Memory Sync (Optional)**: A low-frequency task can be added to periodically sync newly activated proxies (where is\_active becomes true in the database) into the in-memory ActiveProxySet.  
3. **API Business Workflow (Optimized)**  
   1. A client requests /get-proxy?source=my\_app.  
   2. The API service performs sorting and filtering **directly on the in-memory** source\_stats\['my\_app'\] data structure and quickly returns an optimal proxy. **This process does not access the database.**  
   3. The client submits a POST /feedback.  
   4. The API service updates the corresponding proxy's score and success/failure counts **directly in memory**. **This process does not access the database.**

## 5\. Answering Key Questions (New Approach)

1. **Validation Trigger Mechanism**:  
   * **Solution**: **Fixed-Rate Scheduling**. The scheduler acts like a metronome, signaling "start validation" precisely every 5 minutes, regardless of how long the previous validation took. This guarantees a **constant trigger frequency** and solves the schedule drift problem. If a validation cycle takes longer than the interval, the next one will be queued and will start as soon as the current one finishes, ensuring no work is lost while maintaining a predictable rhythm.  
2. **Feedback Performance Bottleneck**:  
   * **Solution**: **Handle all feedback entirely in memory**. This is the core of this architectural upgrade. All scoring and statistical updates are performed in-memory, which is extremely fast and completely eliminates the risk of the database becoming a bottleneck. The potential loss of feedback data between snapshots during a crash is considered an acceptable trade-off for extremely high runtime performance.  
3. **Data Deletion Policy**:  
   * **Solution**: **Logical delete, never physical delete**. Records in the proxies table are no longer deleted. We only update the is\_active field. This satisfies the requirement to retain historical data while ensuring correct business logic through WHERE is\_active \= true query conditions.
  