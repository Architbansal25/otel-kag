package com.nagarro.demo.notification;

import io.swagger.v3.oas.annotations.OpenAPIDefinition;
import io.swagger.v3.oas.annotations.info.Info;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.jms.annotation.EnableJms;
import org.springframework.web.client.RestClient;

import java.time.Duration;

@OpenAPIDefinition(info = @Info(title = "notification-svc", version = "1.0.0",
        description = "Consumes order events and notifies customers."))
@SpringBootApplication
@EnableJms
public class NotificationApplication {

    public static void main(String[] args) {
        SpringApplication.run(NotificationApplication.class, args);
    }

    @Bean
    RestClient inventoryClient(RestClient.Builder builder,
                               @org.springframework.beans.factory.annotation.Value("${demo.inventory-url}") String baseUrl) {
        var factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(Duration.ofSeconds(1));
        factory.setReadTimeout(Duration.ofSeconds(5));
        return builder.baseUrl(baseUrl).requestFactory(factory).build();
    }
}
