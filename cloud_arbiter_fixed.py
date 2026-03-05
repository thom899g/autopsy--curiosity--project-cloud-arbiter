#!/usr/bin/env python3
"""
PROJECT CLOUD-ARBITER v2.0: Enterprise-grade AWS Spot Instance Arbitrage System
Autonomous monitoring of AWS Spot price anomalies and capacity optimization opportunities.
Features: Multi-region monitoring, real-time deviation detection, Firestore state persistence,
Telegram critical alerts, comprehensive error handling, and architectural resilience.

ARCHITECTURAL DECISIONS:
1. Singleton AWS client pattern - Reduces API call overhead and connection pooling
2. Lazy initialization with validation - Prevents runtime failures from missing credentials
3. Exponential backoff for AWS throttling - Ensures reliability during API rate limits
4. Firestore optimistic locking - Prevents race conditions in distributed deployments
5. Structured logging with correlation IDs - Enables distributed tracing in microservices
"""

import os
import sys
import logging
import json
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict
from enum import Enum
from decimal import Decimal
import time
from collections import defaultdict

import boto3
from botocore.exceptions import (
    ClientError, 
    NoCredentialsError, 
    PartialCredentialsError,
    EndpointConnectionError,
    NoRegionError
)
from boto3.exceptions import Boto3Error
import firebase_admin
from firebase_admin import credentials, firestore, exceptions as firebase_exceptions
import requests
from requests.exceptions import RequestException, Timeout, ConnectionError as ReqConnectionError

# === ENHANCED LOGGING CONFIGURATION ===
class CorrelationFilter(logging.Filter):
    """Adds correlation ID for distributed tracing"""
    def filter(self, record):
        record.correlation_id = getattr(self, 'correlation_id', 'default')
        return True

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(correlation_id)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/var/log/cloud-arbiter.log')
    ]
)

logger = logging.getLogger(__name__)
correlation_filter = CorrelationFilter()
correlation_filter.correlation_id = f"arbiter-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
logger.addFilter(correlation_filter)

# === CONFIGURATION WITH VALIDATION ===
class Config:
    """Validated configuration with environment fallbacks"""
    
    @staticmethod
    def get_aws_regions() -> List[str]:
        """Get AWS regions with validation"""
        regions_str = os.getenv('AWS_REGIONS', 'us-east-1,us-west-2,eu-west-1,ap-southeast-1')
        regions = [r.strip() for r in regions_str.split(',')]
        
        valid_regions = [
            'us-east-1', 'us-west-2', 'eu-west-1', 'ap-southeast-1',
            'eu-central-1', 'ap-northeast-1', 'sa-east-1'
        ]
        
        validated = [r for r in regions if r in valid_regions]
        if not validated:
            logger.warning("No valid regions found, using defaults")
            return ['us-east-1', 'us-west-2']
        
        return validated
    
    @staticmethod
    def get_instance_types() -> List[str]:
        """Get instance types with family validation"""
        instances_str = os.getenv('AWS_INSTANCE_TYPES', 't3.micro,t3.small,t3.medium')
        instances = [i.strip() for i in instances_str.split(',')]
        
        # Validate instance type patterns
        valid_families = ['t3', 't4g', 'm5', 'm6g', 'c5', 'c6g']
        validated = []
        
        for instance in instances:
            if any(instance.startswith(family) for family in valid_families):
                validated.append(instance)
            else:
                logger.warning(f"Instance type {instance} may not support spot pricing")
        
        return validated or ['t3.micro', 't3.small']
    
    @staticmethod
    def get_price_threshold() -> float:
        """Get price deviation threshold with bounds checking"""
        try:
            threshold = float(os.getenv('PRICE_THRESHOLD_PERCENT', 20.0))
            if threshold < 5.0:
                logger.warning(f"Threshold {threshold}% too low, minimum 5% enforced")
                return 5.0
            if threshold > 50.0:
                logger.warning(f"Threshold {threshold}% too high, maximum 50% enforced")
                return 50.0
            return threshold
        except ValueError:
            logger.error("Invalid threshold format, using default 20%")
            return 20.0
    
    @staticmethod
    def get_telegram_credentials() -> Tuple[Optional[str], Optional[str]]:
        """Get Telegram credentials with validation"""
        token = os.getenv('TELEGRAM_BOT_TOKEN')
        chat_id = os.getenv('TELEGRAM_CHAT_ID')
        
        if not token or not chat_id:
            logger.warning("Telegram credentials incomplete. Alerts disabled.")
            return None, None
        
        # Basic validation
        if not token.startswith('bot'):
            logger.warning("Telegram token format may be incorrect")
        
        return token, chat_id
    
    @staticmethod
    def get_firebase_creds_path() -> Optional[str]:
        """Get Firebase credentials path with existence