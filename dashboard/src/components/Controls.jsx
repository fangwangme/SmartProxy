import React, { useEffect } from 'react';
import { toLocalYYYYMMDD } from '../utils/dateUtils';

const Controls = ({
    sources, selectedSource, setSelectedSource,
    selectedDate, setSelectedDate,
    loading, handleSearch,
    autoRefresh, setAutoRefresh
}) => {

    const handleDateChange = (offset) => {
        const parts = selectedDate.split('-');
        // Create date in local time (using constructor with year, month index, day)
        // Note: Month is 0-indexed
        const current = new Date(parseInt(parts[0]), parseInt(parts[1]) - 1, parseInt(parts[2]));

        current.setDate(current.getDate() + offset);

        const today = new Date();
        today.setHours(0, 0, 0, 0);

        // Prevent selecting future dates
        if (current > today) {
            return;
        }

        setSelectedDate(toLocalYYYYMMDD(current));
    };

    const isLatestDate = selectedDate === toLocalYYYYMMDD(new Date());

    // Keyboard support for date switching
    useEffect(() => {
        const handleKeyDown = (e) => {
            // Only trigger if no input is focused (optional, but good practice usually, though date picker is focusable)
            // Let's just allow it globally for now as requested.
            if (e.key === 'ArrowLeft') {
                handleDateChange(-1);
            } else if (e.key === 'ArrowRight') {
                handleDateChange(1);
            }
        };

        window.addEventListener('keydown', handleKeyDown);
        return () => window.removeEventListener('keydown', handleKeyDown);
    }, [selectedDate]); // Re-bind on state change to capture latest date

    return (
        <div className="bg-gray-800 p-2 rounded-lg mb-2 flex flex-wrap items-end gap-4 shadow-lg">
            <div className="flex flex-col">
                <label htmlFor="source-select" className="block text-sm font-medium text-gray-300 mb-1">Source</label>
                <select
                    id="source-select"
                    value={selectedSource}
                    onChange={(e) => setSelectedSource(e.target.value)}
                    className="w-48 bg-gray-700 border-gray-600 rounded-md shadow-sm pl-3 pr-8 py-1 text-white focus:outline-none focus:ring-2 focus:ring-cyan-500"
                >
                    {sources.map(source => <option key={source} value={source}>{source}</option>)}
                </select>
            </div>

            <div className="flex flex-col">
                <label htmlFor="date-picker" className="block text-sm font-medium text-gray-300 mb-1">Date</label>
                <div className="flex items-center gap-4">
                    <button
                        onClick={() => handleDateChange(-1)}
                        className="px-6 py-1 bg-gray-700 text-gray-300 hover:text-white hover:bg-gray-600 rounded-md transition-colors shadow-sm"
                        aria-label="Previous Day"
                    >
                        &lt;
                    </button>
                    <input
                        type="date"
                        id="date-picker"
                        value={selectedDate}
                        onChange={(e) => setSelectedDate(e.target.value)}
                        className="bg-gray-700 border-none text-white focus:ring-2 focus:ring-cyan-500 rounded-md px-4 py-1 w-40 text-center shadow-sm"
                    />
                    <button
                        onClick={() => handleDateChange(1)}
                        disabled={isLatestDate}
                        className={`px-6 py-1 rounded-md transition-colors shadow-sm ${isLatestDate ? 'bg-gray-800 text-gray-600 cursor-not-allowed' : 'bg-gray-700 text-gray-300 hover:text-white hover:bg-gray-600'}`}
                        aria-label="Next Day"
                    >
                        &gt;
                    </button>
                </div>
            </div>

            <div className="flex-shrink-0 flex items-center gap-4 mb-0.5">
                <button
                    onClick={handleSearch}
                    disabled={loading}
                    className="bg-cyan-600 hover:bg-cyan-700 disabled:bg-gray-600 disabled:cursor-not-allowed text-white font-bold py-1 px-6 rounded-md transition-colors duration-200 flex items-center justify-center"
                >
                    {loading ? (
                        <>
                            <svg className="animate-spin -ml-1 mr-3 h-5 w-5 text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                            </svg>
                            Loading...
                        </>
                    ) : "Search"}
                </button>

                <div className="flex items-center gap-2 ml-2">
                    <label className="flex items-center cursor-pointer select-none">
                        <div className="relative">
                            <input type="checkbox" className="sr-only" checked={autoRefresh} onChange={() => setAutoRefresh(!autoRefresh)} />
                            <div className={`block w-10 h-6 rounded-full transition-colors ${autoRefresh ? 'bg-cyan-600' : 'bg-gray-600'}`}></div>
                            <div className={`dot absolute left-1 top-1 bg-white w-4 h-4 rounded-full transition-transform ${autoRefresh ? 'transform translate-x-4' : ''}`}></div>
                        </div>
                        <div className="ml-3 text-gray-300 font-medium text-sm">Auto-refresh (30s)</div>
                    </label>
                </div>
            </div>
        </div>
    );
};

export default Controls;
