# **Smart Proxy Server**

A smart, self-managing proxy server built with Flask. It dynamically manages a list of proxies, tracks their performance for different target sources, and provides the best available proxy via a simple API. The server now features a robust state persistence mechanism using Pickle and dynamic configuration for target sources.

## **Features**

* **Dynamic Proxy Loading**: Load and merge new proxies from a text file without restarting the server.  
* **Config-Driven Sources**: Target sources are now loaded from a configuration file (config.ini), removing the need for hard-coded values.  
* **Dynamic Source Management**: Add new target sources on-the-fly via a dedicated API endpoint.  
* **Performance Tracking**: Monitors success rates, failure rates, and average response times for each proxy against each target source.  
* **Automatic Banning**: Proxies that fail consecutively for a specific source are automatically banned for that source.  
* **Intelligent Revival**: When the pool of available proxies for a source is low, the server attempts to revive a banned proxy, prioritizing those with a history of success.  
* **Complete State Persistence**: Saves the entire application state (all proxies, stats, sources, etc.) to a proxy\_manager.pkl file using Pickle, ensuring a seamless restart.  
* **Simple REST API**: Easy to integrate with any client application.

## **Requirements**

* Python 3.6+  
* Flask

pip install Flask

## **Setup & Usage**

1. **Clone or download the project.**  
2. **Run the server for the first time:**  
   python smart\_proxy.py

   The server will start on http://0.0.0.0:6942. On the first run, it will automatically create a data/ directory with the following files:  
   * proxies.txt: A placeholder file for you to add your proxies.  
   * config.ini: The configuration file for target sources, initialized with defaults.  
   * proxy\_manager.pkl: Will be created when the service is shut down or stats are exported.  
3. Populate data/proxies.txt:  
   Add your proxies, one per line, in the format protocol:host:port.  
   \# Example:  
   http:1.2.3.4:8080  
   socks5:9.10.11.12:1080

4. (Optional) Edit data/config.ini:  
   You can pre-populate your list of target sources in this file.  
   \[sources\]  
   default\_sources \= insolvencydirect,test,another\_source

5. **Restart the server** to apply your changes.

## **API Endpoints**

### **1\. Get a Proxy**

* **Endpoint**: GET /get-proxy  
* **Description**: Retrieves an available proxy for a specific target source.  
* **Query Parameters**:  
  * source (required): A unique name for the target service (e.g., insolvencydirect).  
* **Example Request**:  
  curl "http://127.0.0.1:6942/get-proxy?source=insolvencydirect"

### **2\. Submit Feedback**

* **Endpoint**: POST /feedback  
* **Description**: Submits feedback on the performance of a proxy. This is crucial for the server to learn.  
* **Request Body** (JSON):  
  * source (string): The name of the target source.  
  * proxy (string): The full proxy URL that was used.  
  * status (string): Either "success" or "failure".  
  * response\_time\_ms (integer): Required if status is success.  
* **Example Success Feedback**:  
  curl \-X POST \-H "Content-Type: application/json" \\  
       \-d '{"source": "insolvencydirect", "proxy": "http://1.2.3.4:8080", "status": "success", "response\_time\_ms": 250}' \\  
       http://127.0.0.1:6942/feedback

### **3\. Add a New Source (New)**

* **Endpoint**: POST /add-source  
* **Description**: Dynamically adds a new target source to the proxy manager and saves it to config.ini.  
* **Request Body** (JSON):  
  * source (string): The new source name to add.  
* **Example Request**:  
  curl \-X POST \-H "Content-Type: application/json" \\  
       \-d '{"source": "new-api-service"}' \\  
       http://127.0.0.1:6942/add-source

### **4\. Reload Proxies**

* **Endpoint**: POST /load-proxies 
* **Description**: Triggers a hot reload of the `data/proxies.txt` file. The server will read the file and merge any new, unique proxies into its memory.  
* **Example Request**:  
  curl -X POST http://127.0.0.1:6942/load-proxies

### **5\. Get Source Statistics**

* **Endpoint**: GET /stats/\<source\>  
* **Description**: Retrieves the count and list of available proxies for a specific source.  
* **Example Request**:  
  curl http://127.0.0.1:6942/stats/insolvencydirect

### **6\. Export State (Updated)**

* **Endpoint**: GET /export-stats  
* **Description**: Manually triggers the saving of the entire current application state to data/proxy\_manager.pkl. This also happens automatically on graceful shutdown (Ctrl+C).  
* **Example Request**:  
  curl http://127.0.0.1:6942/export-stats

## **How It Works**

### **State Management and Startup**

The server's startup process is designed for robustness and persistence:

1. **Pickle First**: On startup, the server first attempts to load its entire previous state from data/proxy\_manager.pkl. If successful, the server is instantly restored to where it left off.  
2. **Merge New Proxies**: Regardless of whether the pickle load was successful, the server then reads data/proxies.txt. Any proxies found in this file that are not already in memory are added as new, fresh proxies to all known sources.  
3. **Fallback Initialization**: If proxy\_manager.pkl does not exist or fails to load, the server falls back to a clean start:  
   * It reads the list of target sources from data/config.ini.  
   * It loads the initial set of proxies from data/proxies.txt.

### **Proxy Lifecycle**

* When you request a proxy for a source, one is chosen randomly from its available pool.  
* After you use the proxy, you send success or failure feedback.  
* On failure, the proxy's consecutive failure count for that source is incremented. If it exceeds CONSECUTIVE\_FAILURE\_THRESHOLD, the proxy is marked as "banned" for that source and removed from its available pool.  
* On success, the failure count is reset to 0\. If the proxy was previously banned, it is reinstated.  
* If a source's available pool size drops below MIN\_POOL\_SIZE, the server tries to "revive" a banned proxy, prioritizing ones that have succeeded in the past. If none have, it will revive a random banned proxy as a last resort.
