import React, { useState, useEffect, useCallback } from 'react';
import Header from './components/Header';
import Controls from './components/Controls';
import StatsCards from './components/StatsCards';
import Charts from './components/Charts';
import { useDashboardData } from './hooks/useDashboardData';
import { toLocalYYYYMMDD } from './utils/dateUtils';

function App() {
  const {
    sources,
    dailyStats,
    setDailyStats,
    timeseriesData,
    setTimeseriesData,
    loading,
    error,
    setError,
    fetchData
  } = useDashboardData();

  const [selectedSource, setSelectedSource] = useState('');
  const [selectedDate, setSelectedDate] = useState(toLocalYYYYMMDD(new Date()));
  const [interval, setIntervalVal] = useState(5); // Default 5 minutes
  const [autoRefresh, setAutoRefresh] = useState(true); // Default auto-refresh ON

  const validIntervals = [2, 5, 10, 30, 60];

  // Set default source when sources are loaded
  useEffect(() => {
    if (sources.length > 0 && !selectedSource) {
      setSelectedSource(sources[0]);
    }
  }, [sources, selectedSource]);

  // Handle manual search
  const handleSearch = useCallback(() => {
    if (!selectedSource) {
      setError("Please select a source first.");
      return;
    }
    // Clear previous data for visual feedback on manual search? 
    // Maybe better to keep it and just show loading spinner on button.
    // The hook handles loading state.
    setDailyStats(null);
    setTimeseriesData([]);
    fetchData(selectedSource, selectedDate, interval);
  }, [selectedSource, selectedDate, interval, fetchData, setDailyStats, setTimeseriesData, setError]);



  useEffect(() => {
    if (selectedSource && selectedDate) {
      fetchData(selectedSource, selectedDate, interval);
    }
  }, [selectedSource, selectedDate, interval, fetchData]);


  // Auto-refresh logic (30s)
  useEffect(() => {
    let intervalId;
    if (autoRefresh && selectedSource && selectedDate) {
      intervalId = setInterval(() => {
        // Silent fetch (maybe don't clear data?)
        // fetchData handles loading state which might trigger spinner. 
        // That's fine for now.
        fetchData(selectedSource, selectedDate, interval);
      }, 30000);
    }
    return () => clearInterval(intervalId);
  }, [autoRefresh, selectedSource, selectedDate, interval, fetchData]);


  return (
    <div className="bg-gray-900 text-white min-h-screen font-sans p-4 sm:p-6 lg:p-8">
      <div className="max-w-7xl mx-auto">
        <Header />

        {error && (
          <div className="bg-red-900 border border-red-600 text-red-200 px-4 py-3 rounded-lg relative mb-6" role="alert">
            <strong className="font-bold">Error: </strong>
            <span className="block sm:inline">{error}</span>
          </div>
        )}

        <Controls
          sources={sources}
          selectedSource={selectedSource}
          setSelectedSource={setSelectedSource}
          selectedDate={selectedDate}
          setSelectedDate={setSelectedDate}
          loading={loading}
          handleSearch={handleSearch}
          autoRefresh={autoRefresh}
          setAutoRefresh={setAutoRefresh}
        />

        <StatsCards dailyStats={dailyStats} />

        <Charts
          timeseriesData={timeseriesData}
          loading={loading}
          interval={interval}
          setInterval={setIntervalVal}
          validIntervals={validIntervals}
          dailyStats={dailyStats}
        />
      </div>
    </div>
  );
}

export default App;
