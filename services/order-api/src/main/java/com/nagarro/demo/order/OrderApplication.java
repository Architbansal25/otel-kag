package com.nagarro.demo.order;

import io.swagger.v3.oas.annotations.OpenAPIDefinition;
import io.swagger.v3.oas.annotations.info.Info;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.web.client.RestClientCustomizer;
import org.springframework.context.annotation.Bean;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.web.client.RestClient;

import java.time.Duration;

@OpenAPIDefinition(info = @Info(title = "order-api", version = "1.0.0",
        description = "Customer-facing order API. Calls inventory-svc and publishes order events."))
@SpringBootApplication
public class OrderApplication {

    public static void main(String[] args) {
        SpringApplication.run(OrderApplication.class, args);
    }

    /**
     * The downstream client. The 2s read timeout is deliberately SHORTER than
     * the time inventory-svc can spend waiting on its pool: under pool starvation this
     * service gives up first, so the visible failure is a 504 here while the
     * actual cause stays buried two hops away. That asymmetry is the demo.
     */
    @Bean
    RestClient inventoryClient(RestClient.Builder builder,
                               @org.springframework.beans.factory.annotation.Value("${demo.inventory-url}") String baseUrl) {
        var factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(Duration.ofSeconds(1));
        factory.setReadTimeout(Duration.ofSeconds(2));
        return builder.baseUrl(baseUrl).requestFactory(factory).build();
    }
}
