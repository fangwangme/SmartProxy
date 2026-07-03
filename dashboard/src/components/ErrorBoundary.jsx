import React from 'react';

class ErrorBoundary extends React.Component {
    constructor(props) {
        super(props);
        this.state = { hasError: false };
    }

    static getDerivedStateFromError() {
        return { hasError: true };
    }

    componentDidCatch(error, info) {
        console.error('Dashboard render error:', error, info);
    }

    render() {
        if (this.state.hasError) {
            return (
                <div className="bg-red-900 border border-red-600 text-red-200 px-4 py-3 rounded-lg mb-6" role="alert">
                    <strong className="font-bold">Error: </strong>
                    <span className="block sm:inline">Dashboard rendering failed.</span>
                </div>
            );
        }

        return this.props.children;
    }
}

export default ErrorBoundary;
