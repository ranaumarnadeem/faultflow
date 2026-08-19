// soc3_controller.v -- SoC3's UART<->AES handshake FSM, extracted as its own
// wrapped block for the same reason as SoC1's soc1_ram: bare stateful glue
// logic survives assemble_soc's graybox EXTEST composition as real, unfused
// flip-flops and trips the native ATPG engine's combinational-only check.
// Behavior is unchanged from the version originally inline in
// soc3_tinyaes_uart.v: shift 32 bytes in (16-byte plaintext + 16-byte key),
// let the aes_128 pipeline settle, shift 16 bytes of ciphertext back out.
`default_nettype none

module soc3_controller (
	input  wire         clk,
	input  wire         resetn,

	input  wire [127:0] aes_out,
	output wire [127:0] aes_state_in,
	output wire [127:0] aes_key_in,

	input  wire [31:0]  uart_rdata,
	input  wire         uart_tx_wait,
	output reg          uart_re,
	output reg          uart_we,
	output reg  [31:0]  uart_wdata
);

	localparam integer NBYTES_IN    = 32; // 16 state bytes + 16 key bytes
	localparam integer NBYTES_OUT   = 16; // 16 ciphertext bytes
	localparam integer PIPE_LATENCY = 16; // cycles for the aes_128 pipeline to settle

	localparam [1:0] RECV  = 2'd0;
	localparam [1:0] FLUSH = 2'd1;
	localparam [1:0] SEND  = 2'd2;

	reg [1:0]   phase;
	reg [5:0]   byte_idx;
	reg [255:0] in_shift;
	reg [7:0]   flush_cnt;
	reg [127:0] out_shift;

	assign aes_state_in = in_shift[255:128];
	assign aes_key_in   = in_shift[127:0];

	wire uart_byte_ready = !uart_rdata[31];
	wire [7:0] uart_rx_byte = uart_rdata[7:0];

	always @(posedge clk) begin
		uart_re <= 1'b0;
		uart_we <= 1'b0;

		if (!resetn) begin
			phase      <= RECV;
			byte_idx   <= 6'd0;
			in_shift   <= 256'd0;
			flush_cnt  <= 8'd0;
			out_shift  <= 128'd0;
			uart_wdata <= 32'd0;
		end else begin
			case (phase)
				RECV: begin
					uart_re <= 1'b1;
					if (uart_byte_ready) begin
						in_shift <= {in_shift[247:0], uart_rx_byte};
						if (byte_idx == NBYTES_IN - 1) begin
							byte_idx <= 6'd0;
							phase    <= FLUSH;
						end else begin
							byte_idx <= byte_idx + 1'b1;
						end
					end
				end

				FLUSH: begin
					if (flush_cnt == PIPE_LATENCY - 1) begin
						flush_cnt <= 8'd0;
						out_shift <= aes_out;
						phase     <= SEND;
					end else begin
						flush_cnt <= flush_cnt + 1'b1;
					end
				end

				SEND: begin
					if (!uart_tx_wait && !uart_we) begin
						uart_we    <= 1'b1;
						uart_wdata <= {24'b0, out_shift[127:120]};
						out_shift  <= {out_shift[119:0], 8'b0};
						if (byte_idx == NBYTES_OUT - 1) begin
							byte_idx <= 6'd0;
							phase    <= RECV;
						end else begin
							byte_idx <= byte_idx + 1'b1;
						end
					end
				end

				default: phase <= RECV;
			endcase
		end
	end

endmodule
