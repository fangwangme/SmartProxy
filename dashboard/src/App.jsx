import React, { useState, useEffect, useCallback } from 'react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts';

// A simple date formatter for the API
const toYYYYMMDD = (date) => date.toISOString().split('T')[0];

function App() {
  const [sources, setSources] = useState([]);
  const [selectedSource, setSelectedSource] = useState('');
  const [selectedDate, setSelectedDate] = useState(toYYYYMMDD(new Date()));
  const [dailyStats, setDailyStats] = useState({ total_requests: 0, total_success: 0, success_rate: 0 });
  const [timeseriesData, setTimeseriesData] = useState([]);
  const [interval, setInterval] = useState(10);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const API_BASE_URL = 'http://127.0.0.1:6942/api';

  // Fetch the list of available sources on component mount
  useEffect(() => {
    const fetchSources = async () => {
      try {
        const response = await fetch(`${API_BASE_URL}/sources`);
        if (!response.ok) throw new Error('Failed to fetch sources');
        const data = await response.json();
        setSources(data);
        if (data.length > 0) {
          setSelectedSource(data[0]);
        }
      } catch (err) {
        setError('Could not connect to the backend. Is the proxy server running?');
        console.error(err);
      }
    };
    fetchSources();
  }, []);

  // Fetch stats whenever the source or date changes
  const fetchStats = useCallback(async () => {
    if (!selectedSource || !selectedDate) return;
    
    setLoading(true);
    setError(null);

    try {
      // Fetch daily summary
      const dailyRes = await fetch(`${API_BASE_URL}/stats/daily?source=${selectedSource}&date=${selectedDate}`);
      if (!dailyRes.ok) throw new Error(`Failed to fetch daily stats for ${selectedSource}`);
      const dailyData = await dailyRes.json();
      setDailyStats(dailyData);

      // Fetch timeseries data
      const timeseriesRes = await fetch(`${API_BASE_URL}/stats/timeseries?source=${selectedSource}&date=${selectedDate}&interval=${interval}`);
      if (!timeseriesRes.ok) throw new Error(`Failed to fetch timeseries data for ${selectedSource}`);
      const tsData = await timeseriesRes.json();
      setTimeseriesData(tsData);

    } catch (err) {
      setError(err.message);
      console.error(err);
    } finally {
      setLoading(false);
    }
  }, [selectedSource, selectedDate, interval]);

  useEffect(() => {
    fetchStats();
  }, [fetchStats]);

  return (
    <div className="bg-gray-900 text-white min-h-screen font-sans p-4 sm:p-6 lg:p-8">
      <div className="max-w-7xl mx-auto">
        {/* Header */}
        <header className="mb-8">
          <h1 className="text-4xl font-bold text-cyan-400">Proxy Service Dashboard</h1>
          <p className="text-gray-400 mt-2">Real-time monitoring of proxy performance statistics.</p>
        </header>

        {error && (
          <div className="bg-red-900 border border-red-600 text-red-200 px-4 py-3 rounded-lg relative mb-6" role="alert">
            <strong className="font-bold">Error: </strong>
            <span className="block sm:inline">{error}</span>
          </div>
        )}

        {/* Controls */}
        <div className="bg-gray-800 p-4 rounded-lg mb-8 flex flex-wrap items-center gap-4 shadow-lg">
          <div className="flex-grow sm:flex-grow-0">
            <label htmlFor="source-select" className="block text-sm font-medium text-gray-300 mb-1">Source</label>
            <select
              id="source-select"
              value={selectedSource}
              onChange={(e) => setSelectedSource(e.target.value)}
              className="w-full bg-gray-700 border-gray-600 rounded-md shadow-sm pl-3 pr-10 py-2 text-white focus:outline-none focus:ring-2 focus:ring-cyan-500"
            >
              {sources.map(source => <option key={source} value={source}>{source}</option>)}
            </select>
          </div>
          <div className="flex-grow sm:flex-grow-0">
            <label htmlFor="date-picker" className="block text-sm font-medium text-gray-300 mb-1">Date</label>
            <input
              type="date"
              id="date-picker"
              value={selectedDate}
              onChange={(e) => setSelectedDate(e.target.value)}
              className="w-full bg-gray-700 border-gray-600 rounded-md shadow-sm px-3 py-2 text-white focus:outline-none focus:ring-2 focus:ring-cyan-500"
            />
          </div>
        </div>

        {/* Daily Stats Cards */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
          <StatCard title="Total Requests" value={loading ? '...' : dailyStats.total_requests.toLocaleString()} />
          <StatCard title="Successful Requests" value={loading ? '...' : dailyStats.total_success.toLocaleString()} />
          <StatCard title="Success Rate" value={loading ? '...' : `${dailyStats.success_rate}%`} />
        </div>

        {/* Timeseries Chart */}
        <div className="bg-gray-800 p-4 sm:p-6 rounded-lg shadow-lg">
          <div className="flex justify-between items-center mb-4">
            <h2 className="text-xl font-semibold text-gray-200">Success Rate Over Time</h2>
            <div className="flex items-center gap-2">
              <span className="text-sm text-gray-400">Interval:</span>
              {[5, 10, 60].map(val => (
                <button
                  key={val}
                  onClick={() => setInterval(val)}
                  className={`px-3 py-1 text-sm rounded-md transition-colors ${interval === val ? 'bg-cyan-600 text-white' : 'bg-gray-700 hover:bg-gray-600'}`}
                >
                  {val}m
                </button>
              ))}
            </div>
          </div>
          <div style={{ width: '100%', height: 400 }}>
            <ResponsiveContainer>
              {loading ? (
                 <div className="flex items-center justify-center h-full text-gray-500">Loading chart data...</div>
              ) : timeseriesData.length > 0 ? (
                <LineChart data={timeseriesData} margin={{ top: 5, right: 20, left: -10, bottom: 5 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#4A5568" />
                  <XAxis dataKey="time" stroke="#A0AEC0" />
                  <YAxis stroke="#A0AEC0" unit="%" domain={[0, 100]} />
                  <Tooltip
                    contentStyle={{ backgroundColor: '#1A202C', border: '1px solid #4A5568' }}
                    labelStyle={{ color: '#E2E8F0' }}
                  />
                  <Legend wrapperStyle={{ color: '#E2E8F0' }} />
                  <Line type="monotone" dataKey="success_rate" name="Success Rate" stroke="#2DD4BF" strokeWidth={2} dot={false} />
                </LineChart>
              ) : (
                <div className="flex items-center justify-center h-full text-gray-500">No data available for the selected date.</div>
              )}
            </ResponsiveContainer>
          </div>
        </div>
      </div>
    </div>
  );
}

const StatCard = ({ title, value }) => (
  <div className="bg-gray-800 p-6 rounded-lg shadow-lg">
    <h3 className="text-gray-400 text-sm font-medium uppercase tracking-wider">{title}</h3>
    <p className="mt-2 text-3xl font-semibold text-white">{value}</p>
  </div>
);

export default App;
