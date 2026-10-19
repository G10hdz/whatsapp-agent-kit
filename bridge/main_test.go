package main

import (
	"net"
	"testing"
)

func TestRESTAddrIsLoopback(t *testing.T) {
	host, p, err := net.SplitHostPort(restAddr(8080))
	if err != nil {
		t.Fatalf("restAddr(8080) no es host:puerto valido: %v", err)
	}
	if p != "8080" {
		t.Fatalf("puerto esperado 8080, obtuvo %q", p)
	}
	ip := net.ParseIP(host)
	if ip == nil {
		t.Fatalf("host %q no es una IP", host)
	}
	if !ip.IsLoopback() {
		t.Fatalf("host %q no es loopback", host)
	}
}
