import React, { useState, useEffect, useCallback } from 'react';
import Header from './components/Header';
import Controls from './components/Controls';
import StatsCards from './components/StatsCards';
import Charts from './components/Charts';
import ErrorBoundary from './components/ErrorBoundary';
import { useDashboardData } from './hooks/useDashboardData';
import { toLocalYYYYMMDD } from './utils/dateUtils';

function App() {
  const {
    sources,
    allSourceOption,
    dailyStats,
    setDailyStats,
    timeseriesData,
    chartSources,
    setTimeseriesData,
    loading,
    isRefreshing,
    error,
    setError,
    fetchData
  } = useDashboardData();

  const [selectedSource, setSelectedSource] = useState('ALL');
  const [selectedDate, setSelectedDate] = useState(toLocalYYYYMMDD(new Date()));
  const [interval, setIntervalVal] = useState(5); // Default 5 minutes
  const [autoRefresh, setAutoRefresh] = useState(true); // Default auto-refresh ON

  const validIntervals = [2, 5, 10, 30, 60];

  // Keep selected source valid when source list changes.
  useEffect(() => {
    if (sources.length > 0 && !sources.includes(selectedSource)) {
      setSelectedSource(allSourceOption);
    }
  }, [sources, selectedSource, allSourceOption]);

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
        fetchData(selectedSource, selectedDate, interval, { silent: true });
      }, 30000);
    }
    return () => clearInterval(intervalId);
  }, [autoRefresh, selectedSource, selectedDate, interval, fetchData]);


  return (
    <div className="bg-gray-900 text-white min-h-screen font-sans p-4 sm:p-6 lg:p-8">
      <div className="max-w-7xl mx-auto">
        <ErrorBoundary>
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
          isRefreshing={isRefreshing}
          handleSearch={handleSearch}
          autoRefresh={autoRefresh}
          setAutoRefresh={setAutoRefresh}
        />

        <StatsCards dailyStats={dailyStats} />

        <Charts
          timeseriesData={timeseriesData}
          selectedSource={selectedSource}
          allSourceOption={allSourceOption}
          chartSources={chartSources}
          loading={loading}
          isRefreshing={isRefreshing}
          interval={interval}
          setInterval={setIntervalVal}
          validIntervals={validIntervals}
          dailyStats={dailyStats}
        />
        </ErrorBoundary>
      </div>
    </div>
  );
}

export default App;
