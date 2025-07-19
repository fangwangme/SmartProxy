# **Smart Proxy Server**

A smart, self-managing proxy server built with Flask. It dynamically manages a list of proxies, tracks their performance for different target sources, and provides the best available proxy via a simple API.

## **Features**

* **Dynamic Proxy Loading**: Load proxies from a text file without restarting the server.  
* **Performance Tracking**: Monitors success rates, failure rates, and average response times for each proxy against each target source.  
* **Automatic Banning**: Proxies that fail consecutively for a specific source are automatically banned for that source.  
* **Intelligent Revival**: When the pool of available proxies for a source is low, the server attempts to revive a banned proxy that has a history of success.  
* **Persistent Statistics**: Saves performance data to a JSON file and reloads it on startup.  
* **Simple REST API**: Easy to integrate with any client application.

## **Requirements**

* Python 3.6+  
* Flask

pip install Flask

## **Setup & Usage**

1. **Clone or download the project.**  
2. **Install dependencies:**  
   pip install \-r requirements.txt

   *(Note: You may need to create a requirements.txt file with Flask in it, or just run pip install Flask)*  
3. Create and populate proxies.txt:  
   Create a file named proxies.txt in the same directory. Add your proxies, one per line, in one of the following formats:  
   \# Format: protocol:host:port  
   \# Or:     protocol:host:port:user:pass

   http:1.2.3.4:8080  
   http:user:pass@5.6.7.8:8888  
   socks5:9.10.11.12:1080

4. **Run the server:**  
   python smart\_proxy\_server.py

   The server will start on http://0.0.0.0:6942.

## **API Endpoints**

### **1\. Get a Proxy**

* **Endpoint**: GET /get-proxy  
* **Description**: Retrieves the best available proxy for a specific target source.  
* **Query Parameters**:  
  * source (required): A unique name for the target service you are trying to access (e.g., google, amazon, api-service-x).  
* **Example Request**:  
  curl "http://127.0.0.1:6942/get-proxy?source=google"

* **Success Response**:  
  {  
    "http": "http://1.2.3.4:8080",  
    "https": "http://1.2.3.4:8080"  
  }

* **Error Response**:  
  {  
    "error": "No available proxy for source 'google' at the moment."  
  }

### **2\. Submit Feedback**

* **Endpoint**: POST /feedback  
* **Description**: Submits feedback on the performance of a proxy after a request has been made. This is crucial for the server to learn and adjust.  
* **Request Body** (JSON):  
  * source (string): The name of the target source.  
  * proxy (string): The full proxy URL that was used.  
  * status (string): Either "success" or "failure".  
  * response\_time\_ms (integer): The time taken for the request in milliseconds. **Required if status is success**.  
* **Example Success Feedback**:  
  curl \-X POST \-H "Content-Type: application/json" \\  
       \-d '{"source": "google", "proxy": "http://1.2.3.4:8080", "status": "success", "response\_time\_ms": 250}' \\  
       http://127.0.0.1:6942/feedback

* **Example Failure Feedback**:  
  curl \-X POST \-H "Content-Type: application/json" \\  
       \-d '{"source": "google", "proxy": "http://1.2.3.4:8080", "status": "failure"}' \\  
       http://127.0.0.1:6942/feedback

### **3\. Reload Proxies**

* **Endpoint**: POST /load-proxies  
* **Description**: Triggers a hot reload of the proxies.txt file, adding new proxies without duplicates.  
* **Example Request**:  
  curl \-X POST http://127.0.0.1:6942/load-proxies

### **4\. Export Statistics**

* **Endpoint**: GET /export-stats  
* **Description**: Manually triggers the saving of the current proxy performance statistics to proxy\_stats.json. This also happens automatically on graceful shutdown (Ctrl+C).  
* **Example Request**:  
  curl http://127.0.0.1:6942/export-stats

## **How It Works**

The server maintains a separate pool of available proxies for each source.

* When you request a proxy for a source, one is chosen randomly from its available pool.  
* After you use the proxy, you send feedback.  
* If the feedback is a failure, the proxy's consecutive failure count for that source is incremented. If it exceeds CONSECUTIVE\_FAILURE\_THRESHOLD, the proxy is marked as "banned" for that source and removed from its available pool.  
* If the feedback is a success, the consecutive failure count is reset to 0\.  
* If a source's available pool size drops below MIN\_POOL\_SIZE, the server will try to find a banned proxy for that source that has a history of at least one success and "revive" it by adding it back to the pool. This gives previously good proxies a second chance.