package com.nagarro.demo.order;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.web.client.RestClientCustomizer;
import org.springframework.context.annotation.Bean;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.web.client.RestClient;

import java.time.Duration;

@SpringBootApplication
public class OrderApplication {

    public static void main(String[] args) {
        SpringApplication.run(OrderApplication.class, args);
    }

    /**
     * The downstream client. The 2s read timeout is deliberately SHORTER than
     * inventory-svc's 3s Hikari connection-timeout: under pool starvation this
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
