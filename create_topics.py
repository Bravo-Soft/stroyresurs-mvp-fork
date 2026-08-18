# create_topics.py
from kafka import KafkaAdminClient
from kafka.admin import NewTopic

def create_kafka_topics():
    """Создание необходимых топиков Kafka"""
    admin_client = KafkaAdminClient(
        bootstrap_servers="192.168.0.123:9092"
    )
    
    topics = [
        NewTopic(
            name="regular_tasks",
            num_partitions=1,  # Одна партиция для гарантии порядка
            replication_factor=1
        ),
        NewTopic(
            name="urgent_tasks",
            num_partitions=1,
            replication_factor=1
        ),
        NewTopic(
            name="task_status",
            num_partitions=1,
            replication_factor=1
        ),
        NewTopic(
            name="pipeline_control",
            num_partitions=1,
            replication_factor=1
        )
    ]
    
    try:
        admin_client.create_topics(new_topics=topics, validate_only=False)
        print("Топики успешно созданы")
    except Exception as e:
        print(f"Ошибка создания топиков: {e}")
    finally:
        admin_client.close()

if __name__ == "__main__":
    create_kafka_topics()