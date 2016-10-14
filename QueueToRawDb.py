#!/usr/bin/env python3

# dataprocess.py

import argparse
import binascii
from cassandra.cluster import Cluster
from cassandra.query import BatchStatement
from cassandra import ConsistencyLevel
from cassandra.cqlengine.columns import Ascii
from cassandra.cqlengine.usertype import UserType
from config import *
import datetime
import logging 
from multiprocessing import Process, Manager
import pika
import sys
import time
from waggle_protocol.protocol.PacketHandler import *
from waggle_protocol.utilities.gPickler import *
#logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.CRITICAL)

sys.path.append("..")
sys.path.append("/usr/lib/waggle/")

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class DataProcess(Process):
    """
        This process handles all data submissions
        is_database_raw is a bool, if True, will write data to raw-db, else to decoded-db)
    """

    def __init__(self, is_database_raw):
        """
            Starts up the Data handling Process
        """
        super(DataProcess,self).__init__()
        
        if is_database_raw:
            self.input_exchange = 'data-pipeline-in'
            self.queue          = 'db-raw'
            self.statement = "INSERT INTO    sensor_data_raw   (node_id, date, plugin_name, plugin_version, plugin_instance, timestamp, parameter, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            self.function_ExtractValuesFromMessage = self.ExtractValuesFromMessage_raw
        else:  
            self.input_exchange = 'plugins-out'
            self.queue          = 'db-decoded'
            self.statement = "INSERT INTO    sensor_data_decoded   (node_id, date, meta_id, timestamp, data_set, sensor, parameter, data, unit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            self.function_ExtractValuesFromMessage = self.ExtractValuesFromMessage_decoded
            
        logger.info("Initializing DataProcess")
        
        # Set up the Rabbit connection
        #self.connection = pika.BlockingConnection(pika.ConnectionParameters(host='localhost'))
        #Connect to rabbitMQ
        while True:
            try:
                self.connection = pika.BlockingConnection(pika_params)
            except Exception as e:
                logger.error("QueueToRawDb: Could not connect to RabbitMQ server \"%s\": %s" % (pika_params.host, e))
                time.sleep(1)
                continue
            break
            
    
        logger.info("Connected to RabbitMQ server \"%s\"" % (pika_params.host))
        self.numInserted = 0
        self.session = None
        self.cluster = None
        self.prepared_statement = None
        
        self.cassandra_connect()
        
        
        self.channel = self.connection.channel()
        self.channel.basic_qos(prefetch_count=1)
        # Declare this process's queue
        self.channel.queue_declare(self.queue)
        
        self.channel.queue_bind(exchange = self.input_exchange,
            queue = self.queue)
        
        try: 
            self.channel.basic_consume(self.callback, queue=self.queue)
        except KeyboardInterrupt:
           logger.info("exiting.")
           sys.exit(0)
        except Exception as e:
           logger.error("error: %s" % (str(e)))

    def callback(self, ch, method, props, body):
        #TODO: this simply drops failed messages, might find a better solution!? Keeping them has the risk of spamming RabbitMQ
        if False:
            print('######################################')
            print('method = ', method)
            print('props = ', props)
            print('body = ', body)
        '''EXAMPLE: 
            props =  <BasicProperties(['app_id=coresense:3', 'content_type=b', 'delivery_mode=2', 'reply_to=0000001e06107d97', 'timestamp=1476135836151', 'type=frame'])>
        '''
        try:
            values = self.function_ExtractValuesFromMessage(props, body)
        except Exception as e:
            values = None
            logger.error('ERROR computing data for insertion into database: %s' % (str(e)))
            logger.error(' method = {}'.format(repr(method)))
            logger.error(' props  = {}'.format(repr(props)))
            logger.error(' body   = {}'.format(repr(body)))
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        try:
            # Send the data off to Cassandra
            self.cassandra_insert(values)
        except Exception as e:    
            logger.error("Error inserting data: %s" % (str(e)))
            return

        ch.basic_ack(delivery_tag = method.delivery_tag)
        if values:
            self.numInserted += 1
            if self.numInserted % 2 == 0:
                logger.debug('  inserted {}'.format(self.numInserted))

    # Parse a message of sensor data and convert to the values to be inserted into a row in the db
    def ExtractValuesFromMessage_raw(props, body):
        versionStrings  = props.app_id.split(':')
        sampleDatetime  = datetime.datetime.utcfromtimestamp(float(props.timestamp) / 1000.0)
        sampleDate      = sampleDatetime.strftime('%Y-%m-%d')
        node_id         = props.reply_to
        #ingest_id       = props.ingest_id ##props.get('ingest_id', 0)
        #print('ingest_id: ', ingest_id)
        plugin_name     = versionStrings[0]
        plugin_version  = versionStrings[1]
        plugin_instance = '0' if (len(versionStrings) < 3) else versionStrings[2]
        timestamp       = int(props.timestamp)
        parameter       = props.type
        data            = str(binascii.hexlify(body))

        values = (node_id, sampleDate, plugin_name, plugin_version, plugin_instance, timestamp, parameter, data)

        if False:
            print('   node_id = ',          node_id         )
            print('   date = ',             sampleDate      )
            #print('   ingest_id = ',        ingest_id       )
            print('   plugin_name = ',      plugin_name     )
            print('   plugin_version = ',   plugin_version  )
            print('   plugin_instance = ',  plugin_instance )
            print('   timestamp = ',        timestamp       )
            print('   parameter = ',        parameter       )
            print('   data = ',             data            )
        return values
                
    def ExtractValuesFromMessage_decoded(props, body):
        #(node_id, date, meta_id, timestamp, data_set, sensor, parameter, data, unit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"

        versionStrings  = props.app_id.split(':')
        sampleDatetime  = datetime.datetime.utcfromtimestamp(float(props.timestamp) / 1000.0)
        sampleDate      = sampleDatetime.strftime('%Y-%m-%d')
        node_id         = props.reply_to
        #ingest_id       = props.ingest_id ##props.get('ingest_id', 0)
        #print('ingest_id: ', ingest_id)
        meta_id         = props.meta_id
        timestamp       = int(props.timestamp)
        data_set        = props.data_set
        sensor          = props.sensor
        parameter       = props.parameter
        data            = str(binascii.hexlify(body))
        unit            = props.unit

        values = (node_id, sampleDate, meta_id, timestamp, data_set, sensor, parameter, data, unit)

        if False:
            print('   node_id = ',          node_id     )
            print('   date = ',             sampleDate  )
            #print('   ingest_id = ',        ingest_id   )
            print('   meta_id = ',          meta_id     )
            print('   timestamp = ',        timestamp   )
            print('   data_set = ',         data_set    )
            print('   sensor = ',           sensor      )
            print('   parameter = ',        parameter   )
            print('   data = ',             data        )
            print('   unit = ',             unit        )
                
    def cassandra_insert(self, values):
    
        if not self.session:
            self.cassandra_connect()
            
        if not self.prepared_statement:
            try: 
                self.prepared_statement = self.session.prepare(self.statement)
            except Exception as e:
                logger.error("Error preparing statement: (%s) %s" % (type(e).__name__, str(e)) )
                raise
        
        logger.debug("inserting: %s" % (str(values)))
        try:
            bound_statement = self.prepared_statement.bind(values)
        except Exception as e:
            logger.error("QueueToRawDb: Error binding cassandra cql statement:(%s) %s -- values was: %s" % (type(e).__name__, str(e), str(values)) )
            raise

        connection_retry_delay = 1
        while True :
            # this is long term storage    
            try:
                self.session.execute(bound_statement)
            except TypeError as e:    
                 logger.error("QueueToRawDb: (TypeError) Error executing cassandra cql statement: %s -- values was: %s" % (str(e), str(values)) )
                 break
            except Exception as e:
                logger.error("QueueToRawDb: Error (type: %s) executing cassandra cql statement: %s -- values was: %s" % (type(e).__name__, str(e), str(values)) )
                if "TypeError" in str(e):
                    logger.debug("detected TypeError, will ignore this message")
                    break
                
                self.cassandra_connect()
                time.sleep(connection_retry_delay)
                if connection_retry_delay < 10:
                    connection_retry_delay += 1
                continue
            
            break
        logger.debug('cassandra_insert() exiting...')

    def cassandra_connect(self):
        for iTry in range(5):
            if self.cluster:
                try:
                    self.cluster.shutdown()
                except:
                    pass
                    
            self.cluster = Cluster(contact_points=[CASSANDRA_HOST])
            self.session = None
            
            iTry2 = 0
            while not self.session and iTry2 < 5:
                iTry2 += 1
                try: # Might not immediately connect. That's fine. It'll try again if/when it needs to.
                    self.session = self.cluster.connect('waggle')
                except:
                    logger.warning("QueueToRawDb: WARNING: Cassandra connection to " + CASSANDRA_HOST + " failed.")
                    logger.warning("QueueToRawDb: The process will attempt to re-connect at a later time.")
                if not self.session:
                     time.sleep(3)

    def run(self):
        self.cassandra_connect()
        self.channel.start_consuming()

    def join(self):
        super(DataProcess,self).terminate()
        self.connection.close(0)
        if self.cluster:
            self.cluster.shutdown()
            
   
if __name__ == '__main__':
    argParser = argparse.ArgumentParser()
    argParser.add_argument('database', choices = ['raw', 'decoded'], 
        help = 'which database the data is flowing to')
    args = argParser.parse_args()
    is_database_raw = args.database == 'raw'
    
    p = DataProcess(is_database_raw)
    p.start()
    
    print(__name__ + ': created process ', p)
    time.sleep(10)   
    
    while p.is_alive():
        time.sleep(10)
        
    print(__name__ + ': process is dead, time to die')
    p.join()    
