# **Smart Proxy Server**

This is a high-performance, intelligent proxy service built with Python. This version introduces a major upgrade: a persistent statistics module and a real-time monitoring dashboard built with React.

## **Core Architecture**

* **PostgreSQL Backend**: Stores all discovered proxies and new minute-level performance statistics.  
* **High-Performance Feedback Loop**:  
  * /feedback requests are handled instantly by updating an in-memory buffer.  
  * A background task periodically flushes these aggregated statistics to the PostgreSQL database, ensuring no performance impact on the main request threads.  
* **Real-time Monitoring Dashboard**: A rich frontend application built with React and Vite, providing visual insights into proxy performance.  
* **Fixed-Rate Scheduling**: Background tasks for proxy validation and fetching are triggered at fixed intervals for predictable, reliable execution.  
* **Advanced Logging**: Uses **Loguru** for configurable, high-performance logging with daily file rotation.

## **Features**

* **Persistent Statistics**: All feedback is aggregated and stored in the database by the minute.  
* **Monitoring Dashboard**:  
  * Displays daily total requests, successes, and success rates for each source.  
  * Provides interactive time-series charts showing success rate changes over 5, 10, or 60-minute intervals.  
* **Robust Persistence**: Uses PostgreSQL to store all proxy data.  
* **High-Performance In-Memory Cache**: Manages proxy scores for intelligent selection.  
* **Dynamic Source Fetching & Configuration**: Fetches proxies from multiple sources and allows hot-reloading of the source list.  
* **Simple & Powerful REST API**: Easy to integrate with any client application and provides new endpoints for statistical data.

## **Requirements**

* Python 3.7+  
* PostgreSQL Server  
* Node.js and npm (for building the dashboard)  
* Required Python libraries:  
  pip install requests flask psycopg2-binary loguru flask-cors

  Or install from the file:  
  pip install \-r requirements.txt

## **Setup & Usage**

1. **Clone the project.**  
2. **Set up PostgreSQL:**  
   * Ensure you have a running PostgreSQL server.  
   * Create a database and a user for the service.  
   * Execute the database\_setup.sql script to create the proxies and source\_stats\_by\_minute tables.  
     psql \-U your\_postgres\_user \-d your\_database\_name \-f database\_setup.sql

3. **Configure the Backend:**  
   * Copy or rename config.txt.example to config.txt.  
   * Edit config.txt and fill in your \[database\] connection details.  
   * Adjust other settings as needed.  
4. **Build the Frontend Dashboard:**  
   * Navigate to the dashboard directory: cd dashboard.  
   * Install dependencies: npm install.  
   * Build the static files for production: npm run build.  
   * This will create a dist folder inside dashboard. The Flask server is pre-configured to serve files from this location.  
5. **Run the Server:**  
   * Return to the project root directory.  
   * Start the server:  
     python smart\_proxy.py

   * The server will start (default: http://0.0.0.0:6942).  
   * Open your browser and navigate to http://127.0.0.1:6942 to see the monitoring dashboard.

## **API Endpoints**

### **Core API**

#### **1\. Get a Proxy**

* **Endpoint**: GET /get-proxy  
* **Description**: Retrieves an available proxy for a specific target source.  
* **Query Parameters**: source (required).  
* **Example**: curl "http://127.0.0.1:6942/get-proxy?source=insolvencydirect"

#### **2\. Submit Feedback**

* **Endpoint**: POST /feedback  
* **Description**: Submits feedback on proxy performance. This is crucial for both the in-memory scoring and the persistent statistics.  
* **Request Body** (JSON):  
  * source (string, required)  
  * proxy (string, required): The full proxy URL used.  
  * status\_code (integer, required): The numerical status code of the result. 100 and 7 are treated as success, all others as failure.  
  * response\_time\_ms (integer, optional): Response time for calculating latency bonuses.  
* **Example Success Feedback**:  
  curl \-X POST \-H "Content-Type: application/json" \\  
       \-d '{"source": "insolvencydirect", "proxy": "http://1.2.3.4:8080", "status\_code": 100, "response\_time\_ms": 250}' \\  
       http://127.0.0.1:6942/feedback

#### **3\. Reload Sources**

* **Endpoint**: POST /reload-sources  
* **Description**: Triggers a hot reload of the predefined\_sources list from config.txt.  
* **Example**: curl \-X POST http://127.0.0.1:6942/reload-sources

### **Dashboard API**

#### **1\. Get All Sources**

* **Endpoint**: GET /api/sources  
* **Description**: Returns a list of all predefined source names for populating UI dropdowns.

#### **2\. Get Daily Statistics**

* **Endpoint**: GET /api/stats/daily  
* **Description**: Retrieves the aggregated daily statistics for a given source and date.  
* **Query Parameters**:  
  * source (required)  
  * date (required, format: YYYY-MM-DD)  
* **Example**: curl "http://127.0.0.1:6942/api/stats/daily?source=default\&date=2025-08-02"

#### **3\. Get Time-series Statistics**

* **Endpoint**: GET /api/stats/timeseries  
* **Description**: Retrieves time-series data for success rates, aggregated into intervals.  
* **Query Parameters**:  
  * source (required)  
  * date (required, format: YYYY-MM-DD)  
  * interval (optional, 5, 10, or 60, default: 10\)  
* **Example**: curl "http://127.0.0.1:6942/api/stats/timeseries?source=default\&date=2025-08-02\&interval=5"
