# **Smart Proxy Server **

This is a high-performance, intelligent proxy service built with Python. It has been re-architected to use a PostgreSQL backend for robust data persistence and an in-memory model for high-speed feedback processing and proxy selection.

## **Core Architecture ("State Tiering & Precision Scheduling")**

* **PostgreSQL Backend**: All discovered proxies are stored and managed in a PostgreSQL database. We **never delete** proxy records; instead, we use an is\_active flag, preserving historical data for future analysis.  
* **In-Memory Speed**: All business logic—proxy scoring, success/failure tracking, and selection for API requests—happens entirely in memory. This provides millisecond-level response times for high-frequency operations like /get-proxy and /feedback.  
* **Fixed-Rate Scheduling**: Background tasks, such as proxy validation, are triggered at a fixed interval (e.g., every 120 seconds), ensuring a consistent and predictable rhythm, unaffected by how long the task itself takes to run.  
* **Advanced Logging**: The entire system uses the **Loguru** library for beautiful, configurable, and high-performance logging, including daily file rotation and colored console output.

## **Features**

* **Robust Persistence**: Uses PostgreSQL to store all proxy data.  
* **High-Performance In-Memory Cache**: Manages proxy scores and availability in memory for extreme speed.  
* **Dynamic Source Fetching**: Automatically fetches proxies from multiple sources defined in the configuration file.  
* **Parallel Validation**: Uses a multi-threaded validator to efficiently check proxy liveness and anonymity.  
* **Intelligent Scoring**:  
  * Scores proxies based on success, failure, and latency.  
  * Applies exponential penalties for consecutive failures.  
  * Rewards faster proxies with a latency bonus.  
* **Dynamic Configuration**: Hot-reload the list of target sources without restarting the server.  
* **Simple REST API**: Easy to integrate with any client application.

## **Requirements**

* Python 3.7+  
* PostgreSQL Server  
* Required Python libraries:  
  pip install requests flask psycopg2-binary loguru

  Or install from the file:  
  pip install \-r requirements.txt

## **Setup & Usage**

1. **Clone the project.**  
2. **Set up PostgreSQL:**  
   * Make sure you have a PostgreSQL server running.  
   * Create a database and a user for the proxy service.  
   * Execute the database\_setup.sql script to create the necessary proxies table.  
     psql \-U your\_postgres\_user \-d your\_database\_name \-f database\_setup.sql

3. **Configure the Service:**  
   * Copy or rename config.txt.example to config.txt.  
   * Edit config.txt and fill in your \[database\] connection details.  
   * Adjust scheduler, validator, and proxy source settings as needed.  
4. **Run the Server:**  
   python smart\_proxy.py

   The server will start (default on http://0.0.0.0:6942). It will connect to the database, start its background schedulers, and be ready to serve requests. Logs will be printed to the console and saved to the /usr/local/var/log/ directory (or the directory specified in logger.py).

## **API Endpoints**

### **1\. Get a Proxy**

* **Endpoint**: GET /get-proxy  
* **Description**: Retrieves a random, available proxy from the top-performing pool for a specific target source.  
* **Query Parameters**:  
  * source (required): A unique name for the target service (e.g., insolvencydirect). If the source is not predefined in config.txt, it will map to the default\_source.  
* **Example Request**:  
  curl "http://127.0.0.1:6942/get-proxy?source=insolvencydirect"

### **2\. Submit Feedback**

* **Endpoint**: POST /feedback  
* **Description**: Submits feedback on the performance of a proxy. This is **crucial** for the server to learn and adjust scores.  
* **Request Body** (JSON):  
  * source (string): The name of the target source.  
  * proxy (string): The full proxy URL that was used (e.g., http://1.2.3.4:8080).  
  * status (string): Either "success" or "failure".  
  * response\_time\_ms (integer): The response time in milliseconds. Highly recommended for "success" status to calculate latency bonuses.  
* **Example Success Feedback**:  
  curl \-X POST \-H "Content-Type: application/json" \\  
       \-d '{"source": "insolvencydirect", "proxy": "http://1.2.3.4:8080", "status": "success", "response\_time\_ms": 250}' \\  
       http://127.0.0.1:6942/feedback

### **3\. Reload Sources**

* **Endpoint**: POST /reload-sources  
* **Description**: Triggers a hot reload of the predefined\_sources list from config.txt. This allows you to add or remove target sources without a server restart.  
* **Example Request**:  
  curl \-X POST http://127.0.0.1:6942/reload-sources  
