import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Tuple
from dataclasses import dataclass, asdict
from azure.cosmos.aio import CosmosClient
from azure.storage.blob.aio import BlobServiceClient
from azure.functions import HttpRequest, HttpResponse, TimerRequest
import azure.functions as func
from azure.cosmos import exceptions as cosmos_exceptions

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Data Models
@dataclass
class BillingRecord:
    id: str
    partition_key: str
    created_date: datetime
    last_accessed_date: datetime
    is_archived: bool
    archive_blob_path: Optional[str]
    amount: float
    customer_id: str
    invoice_number: str
    billing_details: Dict[str, Any]
    _ts: Optional[int] = None

    def to_dict(self):
        data = asdict(self)
        # Convert datetime objects to ISO format strings
        data['created_date'] = self.created_date.isoformat()
        data['last_accessed_date'] = self.last_accessed_date.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        # Convert ISO format strings back to datetime objects
        if isinstance(data['created_date'], str):
            data['created_date'] = datetime.fromisoformat(data['created_date'].replace('Z', '+00:00'))
        if isinstance(data['last_accessed_date'], str):
            data['last_accessed_date'] = datetime.fromisoformat(data['last_accessed_date'].replace('Z', '+00:00'))
        return cls(**data)

@dataclass
class ArchivedRecordPointer:
    id: str
    partition_key: str
    created_date: datetime
    archived_date: datetime
    blob_path: str
    customer_id: str
    invoice_number: str
    _ts: Optional[int] = None

    def to_dict(self):
        data = asdict(self)
        data['created_date'] = self.created_date.isoformat()
        data['archived_date'] = self.archived_date.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        if isinstance(data['created_date'], str):
            data['created_date'] = datetime.fromisoformat(data['created_date'].replace('Z', '+00:00'))
        if isinstance(data['archived_date'], str):
            data['archived_date'] = datetime.fromisoformat(data['archived_date'].replace('Z', '+00:00'))
        return cls(**data)

# Main Service Class
class BillingRecordsService:
    def __init__(self):
        self.cosmos_client = None
        self.blob_service_client = None
        self.active_records_container = None
        self.archived_pointers_container = None
        self.archive_blob_container = None
        self._initialized = False

    async def initialize(self):
        """Initialize the service with Azure clients"""
        if self._initialized:
            return

        # Initialize Cosmos DB client
        cosmos_connection_string = os.environ.get('CosmosDBConnectionString')
        if not cosmos_connection_string:
            raise ValueError("CosmosDBConnectionString environment variable is required")
        
        self.cosmos_client = CosmosClient.from_connection_string(cosmos_connection_string)
        
        # Initialize Blob Storage client
        blob_connection_string = os.environ.get('BlobStorageConnectionString')
        if not blob_connection_string:
            raise ValueError("BlobStorageConnectionString environment variable is required")
        
        self.blob_service_client = BlobServiceClient.from_connection_string(blob_connection_string)
        
        # Get containers
        database = self.cosmos_client.get_database_client("BillingDB")
        self.active_records_container = database.get_container_client("ActiveRecords")
        self.archived_pointers_container = database.get_container_client("ArchivedPointers")
        self.archive_blob_container = self.blob_service_client.get_container_client("archived-billing-records")
        
        self._initialized = True
        logger.info("BillingRecordsService initialized successfully")

    async def close(self):
        """Close the service and cleanup resources"""
        if self.cosmos_client:
            await self.cosmos_client.close()
        if self.blob_service_client:
            await self.blob_service_client.close()

    async def get_billing_record(self, record_id: str, partition_key: str) -> Optional[BillingRecord]:
        """Retrieve a billing record (checks both active and archived)"""
        try:
            await self.initialize()
            
            # First, try to get from active records
            active_record = await self._get_active_record(record_id, partition_key)
            if active_record:
                # Update last accessed date
                active_record.last_accessed_date = datetime.utcnow()
                await self.active_records_container.upsert_item(
                    body=active_record.to_dict(),
                    partition_key=partition_key
                )
                return active_record

            # If not found in active records, check archived records
            archived_record = await self._get_archived_record(record_id, partition_key)
            return archived_record

        except Exception as e:
            logger.error(f"Error retrieving billing record {record_id}: {str(e)}")
            raise

    async def _get_active_record(self, record_id: str, partition_key: str) -> Optional[BillingRecord]:
        """Get record from active records container"""
        try:
            response = await self.active_records_container.read_item(
                item=record_id,
                partition_key=partition_key
            )
            return BillingRecord.from_dict(response)
        except cosmos_exceptions.CosmosResourceNotFoundError:
            return None

    async def _get_archived_record(self, record_id: str, partition_key: str) -> Optional[BillingRecord]:
        """Get record from archived storage"""
        try:
            # Get the archived record pointer
            pointer_response = await self.archived_pointers_container.read_item(
                item=record_id,
                partition_key=partition_key
            )
            
            pointer = ArchivedRecordPointer.from_dict(pointer_response)
            
            # Retrieve the full record from blob storage
            blob_client = self.archive_blob_container.get_blob_client(pointer.blob_path)
            blob_data = await blob_client.download_blob()
            blob_content = await blob_data.readall()
            
            archived_record_data = json.loads(blob_content.decode('utf-8'))
            archived_record = BillingRecord.from_dict(archived_record_data)
            
            logger.info(f"Retrieved archived record {record_id} from blob storage")
            return archived_record
            
        except cosmos_exceptions.CosmosResourceNotFoundError:
            return None
        except Exception as e:
            logger.error(f"Error retrieving archived record {record_id}: {str(e)}")
            raise

    async def archive_old_records(self) -> int:
        """Archive old records (to be called by a timer function)"""
        await self.initialize()
        
        cutoff_date = datetime.utcnow() - timedelta(days=90)  # 3 months
        
        query = "SELECT * FROM c WHERE c.created_date < @cutoff_date AND c.is_archived = false"
        parameters = [{"name": "@cutoff_date", "value": cutoff_date.isoformat()}]
        
        archived_count = 0
        
        async for item in self.active_records_container.query_items(
            query=query,
            parameters=parameters,
            enable_cross_partition_query=True
        ):
            record = BillingRecord.from_dict(item)
            await self._archive_record(record)
            archived_count += 1

        logger.info(f"Archived {archived_count} records older than {cutoff_date}")
        return archived_count

    async def _archive_record(self, record: BillingRecord):
        """Archive a single record"""
        try:
            # Create blob path using date-based partitioning
            blob_path = f"{record.created_date.strftime('%Y/%m')}/{record.id}.json"
            blob_client = self.archive_blob_container.get_blob_client(blob_path)

            # Upload record to blob storage
            record_json = json.dumps(record.to_dict(), indent=2)
            await blob_client.upload_blob(record_json, overwrite=True)

            # Create archived record pointer
            archived_pointer = ArchivedRecordPointer(
                id=record.id,
                partition_key=record.partition_key,
                created_date=record.created_date,
                archived_date=datetime.utcnow(),
                blob_path=blob_path,
                customer_id=record.customer_id,
                invoice_number=record.invoice_number
            )

            # Store pointer in Cosmos DB
            await self.archived_pointers_container.create_item(
                body=archived_pointer.to_dict(),
                partition_key=record.partition_key
            )

            # Delete original record from active container
            await self.active_records_container.delete_item(
                item=record.id,
                partition_key=record.partition_key
            )

            logger.debug(f"Successfully archived record {record.id}")

        except Exception as e:
            logger.error(f"Failed to archive record {record.id}: {str(e)}")
            raise

    async def get_billing_records_batch(self, record_ids: List[Tuple[str, str]]) -> List[BillingRecord]:
        """Batch retrieval for multiple records"""
        tasks = [self.get_billing_record(record_id, partition_key) for record_id, partition_key in record_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Filter out None results and exceptions
        valid_records = []
        for result in results:
            if isinstance(result, BillingRecord):
                valid_records.append(result)
            elif isinstance(result, Exception):
                logger.error(f"Error in batch retrieval: {str(result)}")
        
        return valid_records

    async def search_records_by_customer(self, customer_id: str, page_size: int = 50) -> List[BillingRecord]:
        """Search records by customer ID (includes both active and archived)"""
        await self.initialize()
        
        # Get active records
        active_records = await self._search_active_records_by_customer(customer_id, page_size)
        
        # Get archived records
        archived_records = await self._search_archived_records_by_customer(customer_id, page_size)
        
        # Combine and sort by creation date
        all_records = active_records + archived_records
        all_records.sort(key=lambda x: x.created_date, reverse=True)
        
        return all_records[:page_size]

    async def _search_active_records_by_customer(self, customer_id: str, page_size: int) -> List[BillingRecord]:
        """Search active records by customer ID"""
        query = "SELECT * FROM c WHERE c.customer_id = @customer_id ORDER BY c.created_date DESC OFFSET 0 LIMIT @page_size"
        parameters = [
            {"name": "@customer_id", "value": customer_id},
            {"name": "@page_size", "value": page_size}
        ]
        
        results = []
        async for item in self.active_records_container.query_items(
            query=query,
            parameters=parameters,
            enable_cross_partition_query=True
        ):
            results.append(BillingRecord.from_dict(item))
            if len(results) >= page_size:
                break
        
        return results

    async def _search_archived_records_by_customer(self, customer_id: str, page_size: int) -> List[BillingRecord]:
        """Search archived records by customer ID"""
        query = "SELECT * FROM c WHERE c.customer_id = @customer_id ORDER BY c.created_date DESC OFFSET 0 LIMIT @page_size"
        parameters = [
            {"name": "@customer_id", "value": customer_id},
            {"name": "@page_size", "value": page_size}
        ]
        
        results = []
        async for item in self.archived_pointers_container.query_items(
            query=query,
            parameters=parameters,
            enable_cross_partition_query=True
        ):
            pointer = ArchivedRecordPointer.from_dict(item)
            
            # Retrieve the full record from blob storage
            try:
                blob_client = self.archive_blob_container.get_blob_client(pointer.blob_path)
                blob_data = await blob_client.download_blob()
                blob_content = await blob_data.readall()
                
                archived_record_data = json.loads(blob_content.decode('utf-8'))
                archived_record = BillingRecord.from_dict(archived_record_data)
                results.append(archived_record)
                
                if len(results) >= page_size:
                    break
                    
            except Exception as e:
                logger.error(f"Error retrieving archived record from blob {pointer.blob_path}: {str(e)}")
                continue
        
        return results

# Global service instance
billing_service = BillingRecordsService()

# Azure Functions
app = func.FunctionApp()

@app.route(route="billing-records/{record_id}", methods=["GET"])
async def get_billing_record(req: HttpRequest) -> HttpResponse:
    """HTTP trigger function to get a billing record"""
    try:
        record_id = req.route_params.get('record_id')
        partition_key = req.params.get('partitionKey')
        
        if not partition_key:
            return HttpResponse(
                "PartitionKey is required",
                status_code=400
            )
        
        record = await billing_service.get_billing_record(record_id, partition_key)
        
        if not record:
            return HttpResponse(
                "Record not found",
                status_code=404
            )
        
        return HttpResponse(
            json.dumps(record.to_dict(), default=str),
            status_code=200,
            headers={"Content-Type": "application/json"}
        )
        
    except Exception as e:
        logger.error(f"Error retrieving billing record {record_id}: {str(e)}")
        return HttpResponse(
            "Internal server error",
            status_code=500
        )

@app.route(route="billing-records/search", methods=["GET"])
async def search_billing_records(req: HttpRequest) -> HttpResponse:
    """HTTP trigger function to search billing records"""
    try:
        customer_id = req.params.get('customerId')
        if not customer_id:
            return HttpResponse(
                "CustomerId is required",
                status_code=400
            )
        
        page_size_param = req.params.get('pageSize', '50')
        try:
            page_size = int(page_size_param)
        except ValueError:
            page_size = 50
        
        records = await billing_service.search_records_by_customer(customer_id, page_size)
        
        # Convert records to dict format for JSON serialization
        records_data = [record.to_dict() for record in records]
        
        return HttpResponse(
            json.dumps(records_data, default=str),
            status_code=200,
            headers={"Content-Type": "application/json"}
        )
        
    except Exception as e:
        logger.error(f"Error searching billing records: {str(e)}")
        return HttpResponse(
            "Internal server error",
            status_code=500
        )

@app.timer_trigger(schedule="0 0 2 * * 0", arg_name="timer", run_on_startup=False)
async def archive_old_records_timer(timer: TimerRequest) -> None:
    """Timer trigger function to archive old billing records (Weekly at 2 AM on Sunday)"""
    try:
        logger.info("Starting archive process for old billing records")
        archived_count = await billing_service.archive_old_records()
        logger.info(f"Archive process completed successfully. Archived {archived_count} records")
        
    except Exception as e:
        logger.error(f"Error during archive process: {str(e)}")
        raise

@app.function_name(name="cleanup_service")
async def cleanup_service():
    """Cleanup function to close service connections"""
    await billing_service.close()

# Additional utility functions for testing and management
async def create_sample_billing_record(
    record_id: str,
    partition_key: str,
    customer_id: str,
    amount: float,
    invoice_number: str,
    billing_details: Dict[str, Any]
) -> BillingRecord:
    """Create a sample billing record for testing"""
    
    record = BillingRecord(
        id=record_id,
        partition_key=partition_key,
        created_date=datetime.utcnow(),
        last_accessed_date=datetime.utcnow(),
        is_archived=False,
        archive_blob_path=None,
        amount=amount,
        customer_id=customer_id,
        invoice_number=invoice_number,
        billing_details=billing_details
    )
    
    await billing_service.initialize()
    await billing_service.active_records_container.create_item(
        body=record.to_dict(),
        partition_key=partition_key
    )
    
    return record

# Example usage and testing
async def main():
    """Example usage of the billing records service"""
    try:
        # Initialize the service
        await billing_service.initialize()
        
        # Create a sample record
        sample_record = await create_sample_billing_record(
            record_id="sample-001",
            partition_key="customer-123",
            customer_id="customer-123",
            amount=299.99,
            invoice_number="INV-2024-001",
            billing_details={
                "service_type": "premium_subscription",
                "billing_period": "2024-01",
                "tax_amount": 29.99,
                "discount_applied": 0.0
            }
        )
        
        print(f"Created sample record: {sample_record.id}")
        
        # Retrieve the record
        retrieved_record = await billing_service.get_billing_record("sample-001", "customer-123")
        print(f"Retrieved record: {retrieved_record.id if retrieved_record else 'Not found'}")
        
        # Search by customer
        customer_records = await billing_service.search_records_by_customer("customer-123")
        print(f"Found {len(customer_records)} records for customer-123")
        
    except Exception as e:
        logger.error(f"Error in main: {str(e)}")
    finally:
        await billing_service.close()

if __name__ == "__main__":
    asyncio.run(main())
