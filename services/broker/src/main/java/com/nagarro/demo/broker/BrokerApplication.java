package com.nagarro.demo.broker;

import io.swagger.v3.oas.annotations.OpenAPIDefinition;
import io.swagger.v3.oas.annotations.info.Info;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.autoconfigure.jms.artemis.ArtemisConfigurationCustomizer;
import org.springframework.context.annotation.Bean;

/**
 * Stands in for Kafka/RabbitMQ in the demo.
 *
 * Spring Boot's embedded Artemis is in-VM only by default, which is no use when
 * order-api and notification-svc are separate processes. The customizer below
 * adds a TCP acceptor so they can both connect to tcp://localhost:61616.
 */
@OpenAPIDefinition(info = @Info(title = "broker", version = "1.0.0",
        description = "Embedded JMS broker hosting the order.events queue."))
@SpringBootApplication
public class BrokerApplication {

    public static void main(String[] args) {
        SpringApplication.run(BrokerApplication.class, args);
    }

    @Bean
    ArtemisConfigurationCustomizer tcpAcceptorCustomizer() {
        return config -> {
            try {
                config.addAcceptorConfiguration("netty", "tcp://0.0.0.0:61616");
            } catch (Exception e) {
                throw new IllegalStateException("Could not open the Artemis TCP acceptor", e);
            }
            // Demo broker: no auth, nothing written to disk.
            config.setSecurityEnabled(false);
            config.setPersistenceEnabled(false);
        };
    }
}
