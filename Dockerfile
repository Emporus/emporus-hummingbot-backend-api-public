# Start from a base image with Miniconda installed
FROM continuumio/miniconda3

# Accept the private pip index as a build argument
ARG pip_index
ENV PIP_INDEX=$pip_index

# Install system dependencies
RUN apt-get update && \
    apt-get install -y sudo libusb-1.0 python3-dev gcc && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /backend-api

# Copy all necessary files into the image
COPY . .

# Create the Conda environment from the environment.yml file
RUN conda env create -f environment.yml

# Install private pip packages using the injected PIP_INDEX
# Use `conda run` to install within the environment
RUN conda run -n backend-api pip install --extra-index-url "${PIP_INDEX}" --no-cache-dir -U pip && \
    conda run -n backend-api pip install --extra-index-url "${PIP_INDEX}" --no-cache-dir -r requirements.txt

# Optional: clean up Conda package cache to reduce image size
RUN conda clean -afy

# Set the shell to use the new environment for future RUN commands
SHELL ["conda", "run", "-n", "backend-api", "/bin/bash", "-c"]

# Entrypoint command when container starts
ENTRYPOINT ["conda", "run", "--no-capture-output", "-n", "backend-api", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]