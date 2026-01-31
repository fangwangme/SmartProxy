# SmartProxy

## Project Structure

This project primarily resides in the `main` branch. Dependencies are centralized to save disk space.

- **`.shared/`**: Contains shared resources like the Python virtual environment (`.venv`) and Node.js modules.
- **`.venv`**: Symbolic link pointing to `.shared/.venv`.
- **`dashboard/`**: React-based frontend application.

## Development Setup

### Python Backend

1.  **Activate Virtual Environment**:

    ```bash
    source .venv/bin/activate
    # If the symlink is missing, use: source .shared/.venv/bin/activate
    ```

2.  **Install Dependencies** (if needed):

    ```bash
    pip install -r requirements.txt
    ```

3.  **Run Application**:
    ```bash
    python run.py
    ```

### Frontend (Dashboard)

1.  **Navigate to dashboard**:

    ```bash
    cd dashboard
    ```

2.  **Install Dependencies**:
    We use **Bun** for package management.

    ```bash
    bun install
    ```

3.  **Start Dev Server**:
    ```bash
    bun run dev
    ```
