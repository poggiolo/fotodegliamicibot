# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set environment variables
# Prevent Python from writing .pyc files to disc
ENV PYTHONDONTWRITEBYTECODE=1
# Prevent Python from buffering stdout and stderr
ENV PYTHONUNBUFFERED=1

# Set the working directory in the container
WORKDIR /app

# Install dependencies
# Copy only the requirements file first to leverage Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY bot.py immich_client.py ./

# Note: state.json will be created at runtime in /app. 
# To persist it, mount a volume or bind-mount a file to /app/state.json.

# Run the bot
CMD ["python", "bot.py"]
