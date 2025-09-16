# Use an official lightweight Python image as a parent image
FROM python:3.10-slim-bookworm

# Upgrade pip and setuptools to latest versions to reduce vulnerabilities
RUN pip install --upgrade pip setuptools

# Ensure all system packages are up-to-date to reduce vulnerabilities
RUN apt-get update && apt-get dist-upgrade -y && apt-get clean && rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container
WORKDIR /app

# Copy the file that lists the dependencies
COPY requirements.txt .

# Install the dependencies
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
	gcc build-essential libffi-dev && \
	pip install --no-cache-dir -r requirements.txt && \
	apt-get purge -y --auto-remove gcc build-essential libffi-dev && \
	apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy the rest of the application's code into the container
COPY . .

# Command to run when the container starts
CMD ["python", "bot.py"]
